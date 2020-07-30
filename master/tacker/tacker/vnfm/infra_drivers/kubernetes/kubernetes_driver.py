# All Rights Reserved.
#
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import ast
import os
import re
import time
import urllib.request as urllib2
import yaml

from kubernetes import client
from oslo_config import cfg
from oslo_log import log as logging
from oslo_serialization import jsonutils

from tacker._i18n import _
from tacker.common import log
from tacker.common import utils
from tacker.common.container import kubernetes_utils
from tacker.extensions import vnfm
from tacker.objects import vnf_package as vnf_package_obj
from tacker.objects import vnf_package_vnfd as vnfd_obj
from tacker.objects import vnf_resources as vnf_resource_obj
from tacker.vnfm.infra_drivers import abstract_driver
from tacker.vnfm.infra_drivers import scale_driver
from tacker.vnfm.infra_drivers.kubernetes import translate_template
from tacker.vnfm.infra_drivers.kubernetes.k8s import translate_outputs
from urllib.parse import urlparse


LOG = logging.getLogger(__name__)
CONF = cfg.CONF

OPTS = [
    cfg.IntOpt('stack_retries',
               default=100,
               help=_("Number of attempts to retry for stack"
                      " creation/deletion")),
    cfg.IntOpt('stack_retry_wait',
               default=5,
               help=_("Wait time (in seconds) between consecutive stack"
                      " create/delete retries")),
]

CONF.register_opts(OPTS, group='kubernetes_vim')


def config_opts():
    return [('kubernetes_vim', OPTS)]


SCALING_POLICY = 'tosca.policies.tacker.Scaling'
COMMA_CHARACTER = ','


def get_scaling_policy_name(action, policy_name):
    return '%s_scale_%s' % (policy_name, action)


class Kubernetes(abstract_driver.VnfAbstractDriver,
                 scale_driver.VnfScaleAbstractDriver):
    """Kubernetes infra driver for hosting containerized vnfs"""

    def __init__(self):
        super(Kubernetes, self).__init__()
        self.STACK_RETRIES = cfg.CONF.kubernetes_vim.stack_retries
        self.STACK_RETRY_WAIT = cfg.CONF.kubernetes_vim.stack_retry_wait
        self.kubernetes = kubernetes_utils.KubernetesHTTPAPI()
        self.CHECK_DICT_KEY = [
            "Pod",
            "Service",
            "PersistentVolumeClaim",
            "Namespace",
            "Node",
            "PersistentVolume",
            "APIService",
            "DaemonSet",
            "Deployment",
            "ReplicaSet",
            "StatefulSet",
            "Job",
            "VolumeAttachment"
        ]

    def get_type(self):
        return 'kubernetes'

    def get_name(self):
        return 'kubernetes'

    def get_description(self):
        return 'Kubernetes infra driver'

    @log.log
    def create(self, plugin, context, vnf, auth_attr):
        """Create function

        Create ConfigMap, Deployment, Service and Horizontal Pod Autoscaler
        objects. Return a string that contains all deployment namespace and
        names for tracking resources.
        """
        LOG.debug('vnf %s', vnf)
        # initialize Kubernetes APIs
        auth_cred, file_descriptor = self._get_auth_creds(auth_attr)
        try:
            core_v1_api_client = self.kubernetes.get_core_v1_api_client(
                auth=auth_cred)
            app_v1_api_client = self.kubernetes.get_app_v1_api_client(
                auth=auth_cred)
            scaling_api_client = self.kubernetes.get_scaling_api_client(
                auth=auth_cred)
            tosca_to_kubernetes = translate_template.TOSCAToKubernetes(
                vnf=vnf,
                core_v1_api_client=core_v1_api_client,
                app_v1_api_client=app_v1_api_client,
                scaling_api_client=scaling_api_client)
            deployment_names = tosca_to_kubernetes.deploy_kubernetes_objects()
        except Exception as e:
            LOG.error('Creating VNF got an error due to %s', e)
            raise
        finally:
            self.clean_authenticate_vim(auth_cred, file_descriptor)
        return deployment_names

    def create_wait(self, plugin, context, vnf_dict, vnf_id, auth_attr):
        """Create wait function

        Create wait function will marked VNF is ACTIVE when all status state
        from Pod objects is RUNNING.
        """
        # initialize Kubernetes APIs
        # auth_cred, file_descriptor = self._get_auth_creds(auth_attr)
        # try:
        #     core_v1_api_client = \
        #         self.kubernetes.get_core_v1_api_client(auth=auth_cred)
        #     deployment_info = vnf_id.split(COMMA_CHARACTER)
        #     mgmt_ips = dict()
        #     pods_information = self._get_pods_information(
        #         core_v1_api_client=core_v1_api_client,
        #         deployment_info=deployment_info)
        #     status = self._get_pod_status(pods_information)
        #     stack_retries = self.STACK_RETRIES
        #     error_reason = None
        #     while status == 'Pending' and stack_retries > 0:
        #         time.sleep(self.STACK_RETRY_WAIT)
        #         pods_information = \
        #             self._get_pods_information(
        #                 core_v1_api_client=core_v1_api_client,
        #                 deployment_info=deployment_info)
        #         status = self._get_pod_status(pods_information)
        #         LOG.debug('status: %s', status)
        #         stack_retries = stack_retries - 1
        #
        #     LOG.debug('VNF initializing status: %(service_name)s '
        #               '%(status)s',
        #               {'service_name': str(deployment_info),
        #               'status': status})
        #     if stack_retries == 0 and status != 'Running':
        #         error_reason = _(
        #             "Resource creation is not completed within"
        #             " {wait} seconds as creation of stack {stack}"
        #             " is not completed").format(
        #             wait=(
        #                 self.STACK_RETRIES *
        #                 self.STACK_RETRY_WAIT),
        #             stack=vnf_id)
        #         LOG.warning("VNF Creation failed: %(reason)s",
        #                     {'reason': error_reason})
        #         raise vnfm.VNFCreateWaitFailed(reason=error_reason)
        #     elif stack_retries != 0 and status != 'Running':
        #         raise vnfm.VNFCreateWaitFailed(reason=error_reason)
        #
        #     for i in range(0, len(deployment_info), 2):
        #         namespace = deployment_info[i]
        #         deployment_name = deployment_info[i + 1]
        #         service_info = core_v1_api_client.read_namespaced_service(
        #             name=deployment_name,
        #             namespace=namespace)
        #         if service_info.metadata.labels.get(
        #                 "management_connection"):
        #             vdu_name = service_info.metadata.labels.\
        #                 get("vdu_name").split("-")[1]
        #             mgmt_ip = service_info.spec.cluster_ip
        #             mgmt_ips.update({vdu_name: mgmt_ip})
        #             vnf_dict['mgmt_ip_address'] = jsonutils.dump_as_bytes(
        #                 mgmt_ips)
        # except Exception as e:
        #     LOG.error('Creating wait VNF got an error due to %s', e)
        #     raise
        # finally:
        #     self.clean_authenticate_vim(auth_cred, file_descriptor)
        pass

    def create_wait_new(self, k8s_objs, k8s_client_dict):
        try:
            keep_going = True
            stack_retries = self.STACK_RETRIES
            while keep_going and stack_retries > 0:
                for k8s_obj in k8s_objs:
                    kind = k8s_obj.get('objects').kind
                    namespace = k8s_obj.get('namespace')
                    name = k8s_obj.get('objects').metadata.name
                    apiVersion = k8s_obj.get('objects').apiVersion
                    if k8s_obj.get('status') == 'Creating':
                        if kind in self.CHECK_DICT_KEY:
                            check_method = self.\
                                select_check_status_by_kind(kind)
                            if check_method.__code__.co_argcount == 5:
                                check_method(k8s_client_dict, k8s_obj,
                                            name, apiVersion)
                            elif check_method.__code__.co_argcount == 6:
                                check_method(k8s_client_dict, k8s_obj,
                                             namespace, name, apiVersion)
                        else:
                            k8s_obj['status'] = 'Create_complete'

                complete_flag = True
                for k8s_obj in k8s_objs:
                    if k8s_obj.get('status') != 'Create_complete':
                        complete_flag = False
                if not complete_flag:
                    keep_going = True
                    time.sleep(self.STACK_RETRY_WAIT)
                    stack_retries -= 1
                else:
                    keep_going = False
            if stack_retries == 0 and keep_going:
                for k8s_obj in k8s_objs:
                    if k8s_obj.get('status') == 'Creating':
                        k8s_obj['status'] = 'Wait_failed'
                        err_reason = _("The resources are creating time out."
                                       "namespace: {namespace}, name:{name}, "
                                       "kind: {kind}).Reason: {message}").\
                            format(namespace=k8s_obj.get('namespace'),
                                   name=k8s_obj.get('objects').metadata.name,
                                   kind=k8s_obj.get('objects').kind,
                                   message=k8s_obj['message'])
                        LOG.error(err_reason)
                error_reason = _(
                    "Resource creation is not completed within"
                    " {wait} seconds as creation of stack {stack}"
                    " is not completed").format(
                    wait=(
                        self.STACK_RETRIES *
                        self.STACK_RETRY_WAIT))
                raise vnfm.VNFCreateWaitFailed(reason=error_reason)
            return k8s_objs
        except Exception as e:
            LOG.error('Creating wait VNF got an error due to %s', e)
            raise

    def select_check_status_by_kind(self, kind, k8s_client_dict, k8s_obj,
                                    namespace, name, apiVersion):
        check_dict = {
            "Pod": self.check_status_pod,
            "Service": self.check_status_service,
            "PersistentVolumeClaim":
                self.check_status_persistent_volume_claim,
            "Namespace": self.check_status_namespace,
            "Node": self.check_status_node,
            "PersistentVolume": self.check_status_persistent_volume,
            "APIService": self.check_status_api_service,
            "DaemonSet": self.check_status_daemon_set,
            "Deployment": self.check_status_deployment,
            "ReplicaSet": self.check_status_replica_set,
            "StatefulSet": self.check_status_stateful_set,
            "Job": self.check_status_job,
            "VolumeAttachment": self.check_status_volume_attachment
        }
        return check_dict[kind]

    def check_is_ip(self, ip_str):
        if re.match(r'^\d{3}.\d{3}.\d{3}.\d{3}$', ip_str):
            num_list = [int(x) for x in ip_str.split('.')]
            for i in num_list:
                if i > 255 or i < 0:
                    return False
            return True
        else:
            return False

    def check_status_stateful_set(self, k8s_client_dict, k8s_obj,
                                  namespace, name, apiVersion):
        stateful_set = k8s_client_dict[apiVersion].\
            read_namespaced_stateful_set(namespace, name)
        for i in range(0, stateful_set.spec.replicas):
            pvc_name = "-".join(
                stateful_set.spec.volumeClaimTemplates.
                metadata.name, name, str(i))
            persistent_volume_claim = k8s_client_dict[apiVersion].\
                read_namespaced_persistent_volume_claim(namespace, pvc_name)
            if persistent_volume_claim.status.phase != 'Bound':
                k8s_obj['status'] = 'Creating'
                k8s_obj['message'] = "PersistentVolumeClaim in "\
                    "StatefulSet is still creating. It's status is "\
                    "{status}".format(status=persistent_volume_claim.
                                      status.phase)
        stateful_set = k8s_client_dict[apiVersion].\
            read_namespaced_stateful_set(namespace, name)
        if stateful_set.status.replicas != \
                stateful_set.status.ready_replicas:
            k8s_obj['status'] = 'Creating'
            k8s_obj['message'] = "Pod in StatefulSet is still creating. " \
                "The pod is ready {value1}/{value2}".format(
                value1=stateful_set.status.ready_replicas,
                value2=stateful_set.status.replicas
            )
        elif stateful_set.status.replicas == \
                stateful_set.status.ready_replicas:
            k8s_obj['status'] = 'Create_complete'
            k8s_obj['message'] = 'StatefulSet is created'

    def check_status_pod(self, k8s_client_dict, k8s_obj,
                         namespace, name, apiVersion):
        pod = k8s_client_dict[apiVersion].read_namespaced_pod(
            namespace, name)
        if pod.status.phase != 'Running':
            k8s_obj['status'] = 'Creating'
            k8s_obj['message'] = "Pod is still creating.It's status is " \
                                 "{status}".format(status=pod.
                                                   status.phase)
        else:
            k8s_obj['status'] = 'Create_complete'
            k8s_obj['message'] = "Pod is created"

    def check_status_service(self, k8s_client_dict, k8s_obj,
                         namespace, name, apiVersion):
        service = k8s_client_dict[apiVersion].read_namespaced_service(
            namespace, name)
        status_flag = False
        if service.spec.cluster_ip in ['',None] or \
            self.check_is_ip(service.spec.cluster_ip):
            try:
                endpoint = k8s_client_dict[apiVersion].\
                    read_namespaced_endpoints(namespace, name)
                if endpoint:
                    status_flag = True
            except Exception as e:
                pass
        if status_flag:
            k8s_obj['status'] = 'Create_complete'
            k8s_obj['message'] = "Service is created"
        else:
            k8s_obj['status'] = 'Creating'
            k8s_obj['message'] = "Service is still creating." \
                                 "It's status is False"


    def check_status_persistent_volume_claim(self, k8s_client_dict, k8s_obj,
                         namespace, name, apiVersion):
        claim = k8s_client_dict[apiVersion].\
            read_namespaced_persistent_volume_claim(namespace, name)
        if claim.status.phase != 'Bound':
            k8s_obj['status'] = 'Creating'
            k8s_obj['message'] = "PersistentVolumeClaim is still creating."\
                                 "It's status is {status}".\
                format(status=claim.status.phase)
        else:
            k8s_obj['status'] = 'Create_complete'
            k8s_obj['message'] = "PersistentVolumeClaim is created"

    def check_status_namespace(self, k8s_client_dict, k8s_obj,
                         name, apiVersion):
        name_space = k8s_client_dict[apiVersion].read_namespace(name)
        if name_space.status.pahse != 'Active':
            k8s_obj['status'] = 'Creating'
            k8s_obj['message'] = "Namespace is still creating." \
                                 "It's status is {status}". \
                format(status=name_space.status.phase)
        else:
            k8s_obj['status'] = 'Create_complete'
            k8s_obj['message'] = "Namespace is created"

    def check_status_node(self, k8s_client_dict, k8s_obj,
                               name, apiVersion):
        node = k8s_client_dict[apiVersion].read_node(name)
        status_flag = False
        for i in node.status.conditions:
            if i.get('type') == 'Ready':
                if i.get('status') == True:
                    status_flag = True
                    break
            else:
                continue
        if status_flag:
            k8s_obj['status'] = 'Create_complete'
            k8s_obj['message'] = "Node is created"
        else:
            k8s_obj['status'] = 'Creating'
            k8s_obj['message'] = "Node is still creating." \
                                 "It's status is False"

    def check_status_persistent_volume(self, k8s_client_dict, k8s_obj,
                         name, apiVersion):
        volume = k8s_client_dict[apiVersion].\
            read_persistent_volume(name)
        if volume.status.pahse != 'Available':
            k8s_obj['status'] = 'Creating'
            k8s_obj['message'] = "PersistentVolume is still creating." \
                                 "It's status is {status}". \
                format(status=volume.status.phase)
        else:
            k8s_obj['status'] = 'Create_complete'
            k8s_obj['message'] = "PersistentVolume is created"

    def check_status_api_service(self, k8s_client_dict, k8s_obj,
                               name, apiVersion):
        api_service = k8s_client_dict[apiVersion].read_api_service(name)
        status_flag = False
        for i in api_service.status.conditions:
            if i.get('type') == 'Available':
                if i.get('status') == True:
                    status_flag = True
                    break
            else:
                continue
        if status_flag:
            k8s_obj['status'] = 'Create_complete'
            k8s_obj['message'] = "APIService is created"
        else:
            k8s_obj['status'] = 'Creating'
            k8s_obj['message'] = "APIService is still creating." \
                                 "It's status is False"

    def check_status_daemon_set(self, k8s_client_dict, k8s_obj,
                         namespace, name, apiVersion):
        daemon_set = k8s_client_dict[apiVersion].\
            read_namespaced_daemon_set(namespace, name)
        if daemon_set.status.desired_number_scheduled != \
                daemon_set.status.number_ready:
            k8s_obj['status'] = 'Creating'
            k8s_obj['message'] = "DaemonSet is still creating. " \
                                 "The DaemonSet is ready {value1}/{value2}".\
                format(value1=daemon_set.status.number_ready,
                value2=daemon_set.status.desired_number_scheduled
            )
        else:
            k8s_obj['status'] = 'Create_complete'
            k8s_obj['message'] = 'DaemonSet is created'

    def check_status_deployment(self, k8s_client_dict, k8s_obj,
                         namespace, name, apiVersion):
        deployment = k8s_client_dict[apiVersion].\
            read_namespaced_deployment(namespace, name)
        if deployment.status.replicas != deployment.status.ready_replicas:
            k8s_obj['status'] = 'Creating'
            k8s_obj['message'] = "Deployment is still creating. " \
                                 "The Deployment is ready {value1}/{value2}".\
                format(value1=deployment.status.ready_replicas,
                       value2=deployment.status.replicas
                       )
        else:
            k8s_obj['status'] = 'Create_complete'
            k8s_obj['message'] = 'Deployment is created'

    def check_status_replica_set(self, k8s_client_dict, k8s_obj,
                         namespace, name, apiVersion):
        replica_set = k8s_client_dict[apiVersion].\
            read_namespaced_replica_set(namespace, name)
        if replica_set.status.replicas != replica_set.status.ready_replicas:
            k8s_obj['status'] = 'Creating'
            k8s_obj['message'] = "ReplicaSet is still creating. " \
                                 "The ReplicaSet is ready {value1}/{value2}".\
                format(value1=replica_set.status.ready_replicas,
                       value2=replica_set.status.replicas
                       )
        else:
            k8s_obj['status'] = 'Create_complete'
            k8s_obj['message'] = 'ReplicaSet is created'

    def check_status_job(self, k8s_client_dict, k8s_obj,
                         namespace, name, apiVersion):
        job = k8s_client_dict[apiVersion].\
            read_namespaced_job(namespace, name)
        if job.spec.completions != job.status.succeeded:
            k8s_obj['status'] = 'Creating'
            k8s_obj['message'] = "Job is still creating." \
                                 "It's status is {status}". \
                format(status=job.spec.completions)
        else:
            k8s_obj['status'] = 'Create_complete'
            k8s_obj['message'] = 'Job is created'

    def check_status_volume_attachment(self, k8s_client_dict, k8s_obj,
                         name, apiVersion):
        volume = k8s_client_dict[apiVersion].\
            read_volume_attachment(name)
        if volume.status.attached != True:
            k8s_obj['status'] = 'Creating'
            k8s_obj['message'] = "VolumeAttachment is still creating." \
                                 "It's status is {status}". \
                format(status=volume.status.attached)
        else:
            k8s_obj['status'] = 'Create_complete'
            k8s_obj['message'] = 'VolumeAttachment is created'

    def _get_pods_information(self, core_v1_api_client, deployment_info):
        """Get pod information"""
        pods_information = list()
        for i in range(0, len(deployment_info), 2):
            namespace = deployment_info[i]
            deployment_name = deployment_info[i + 1]
            respone = \
                core_v1_api_client.list_namespaced_pod(namespace=namespace)
            for item in respone.items:
                if deployment_name in item.metadata.name:
                    pods_information.append(item)
        return pods_information

    def _get_pod_status(self, pods_information):
        pending_flag = False
        unknown_flag = False
        for pod_info in pods_information:
            status = pod_info.status.phase
            if status == 'Pending':
                pending_flag = True
            elif status == 'Unknown':
                unknown_flag = True
        if unknown_flag:
            status = 'Unknown'
        elif pending_flag:
            status = 'Pending'
        else:
            status = 'Running'
        return status

    @log.log
    def update(self, plugin, context, vnf_id, vnf_dict, vnf, auth_attr):
        """Update containerized VNF through ConfigMap data

        In Kubernetes VIM, updating VNF will be updated by updating
        ConfigMap data
        """
        # initialize Kubernetes APIs
        auth_cred, file_descriptor = self._get_auth_creds(auth_attr)
        try:
            core_v1_api_client = \
                self.kubernetes.get_core_v1_api_client(auth=auth_cred)

            # update config attribute
            config_yaml = vnf_dict.get('attributes', {}).get('config', '')
            update_yaml = vnf['vnf'].get('attributes', {}).get('config', '')
            LOG.debug('yaml orig %(orig)s update %(update)s',
                      {'orig': config_yaml, 'update': update_yaml})
            # If config_yaml is None, yaml.safe_load() will raise Attribute
            # Error. So set config_yaml to {}, if it is None.
            if not config_yaml:
                config_dict = {}
            else:
                config_dict = yaml.safe_load(config_yaml) or {}
            update_dict = yaml.safe_load(update_yaml)
            if not update_dict:
                return
            LOG.debug('dict orig %(orig)s update %(update)s',
                      {'orig': config_dict, 'update': update_dict})
            utils.deep_update(config_dict, update_dict)
            LOG.debug('dict new %(new)s update %(update)s',
                      {'new': config_dict, 'update': update_dict})
            new_yaml = yaml.safe_dump(config_dict)
            vnf_dict.setdefault('attributes', {})['config'] = new_yaml

            deployment_info = vnf_id.split(",")
            for i in range(0, len(deployment_info), 2):
                namespace = deployment_info[i]
                deployment_name = deployment_info[i + 1]
                configmap_resp = core_v1_api_client.read_namespaced_config_map(
                    namespace=namespace,
                    name=deployment_name)
                configmap_data = configmap_resp.data
                new_configmap = {key: update_dict.get(key, configmap_data[key])
                                 for key in configmap_data}
                configmap_resp.data = new_configmap
                core_v1_api_client.\
                    patch_namespaced_config_map(namespace=namespace,
                                                name=deployment_name,
                                                body=configmap_resp)
        except Exception as e:
            LOG.error('Updating VNF got an error due to %s', e)
            raise
        finally:
            self.clean_authenticate_vim(auth_cred, file_descriptor)

    @log.log
    def update_wait(self, plugin, context, vnf_id, auth_attr,
                    region_name=None):
        """Update wait function"""
        # TODO(phuoc): do nothing, will update it if we need actions
        pass

    def delete(self, plugin, context, vnf_id, auth_attr, region_name=None):
        """Delete function"""
        # initialize Kubernetes APIs
        auth_cred, file_descriptor = self._get_auth_creds(auth_attr)
        try:
            core_v1_api_client = self.kubernetes.get_core_v1_api_client(
                auth=auth_cred)
            app_v1_api_client = self.kubernetes.get_app_v1_api_client(
                auth=auth_cred)
            scaling_api_client = self.kubernetes.get_scaling_api_client(
                auth=auth_cred)
            deployment_names = vnf_id.split(COMMA_CHARACTER)

            for i in range(0, len(deployment_names), 2):
                namespace = deployment_names[i]
                deployment_name = deployment_names[i + 1]
                # delete ConfigMap if it exists
                try:
                    body = {}
                    core_v1_api_client.delete_namespaced_config_map(
                        namespace=namespace,
                        name=deployment_name,
                        body=body)
                    LOG.debug('Successfully deleted ConfigMap %s',
                              deployment_name)
                except Exception as e:
                    LOG.debug(e)
                    pass
                # delete Service if it exists
                try:
                    core_v1_api_client.delete_namespaced_service(
                        namespace=namespace,
                        name=deployment_name)
                    LOG.debug('Successfully deleted Service %s',
                              deployment_name)
                except Exception as e:
                    LOG.debug(e)
                    pass
                # delete Horizon Pod Auto-scaling if it exists
                try:
                    body = client.V1DeleteOptions()
                    scaling_api_client.\
                        delete_namespaced_horizontal_pod_autoscaler(
                            namespace=namespace,
                            name=deployment_name,
                            body=body)
                    LOG.debug('Successfully deleted Horizon Pod Auto-Scaling '
                              '%s', deployment_name)
                except Exception as e:
                    LOG.debug(e)
                    pass
                # delete Deployment if it exists
                try:
                    body = client.V1DeleteOptions(
                        propagation_policy='Foreground',
                        grace_period_seconds=5)
                    app_v1_api_client.delete_namespaced_deployment(
                        namespace=namespace,
                        name=deployment_name,
                        body=body)
                    LOG.debug('Successfully deleted Deployment %s',
                              deployment_name)
                except Exception as e:
                    LOG.debug(e)
                    pass
        except Exception as e:
            LOG.error('Deleting VNF got an error due to %s', e)
            raise
        finally:
            self.clean_authenticate_vim(auth_cred, file_descriptor)

    def delete_wait(self, plugin, context, vnf_id, auth_attr,
                    region_name=None):
        """Delete wait function

        This function is used to checking a containerized VNF is deleted
        completely or not. We do it by get information of Kubernetes objects.
        When Tacker can not get any information about service, the VNF will be
        marked as deleted.
        """
        # initialize Kubernetes APIs
        auth_cred, file_descriptor = self._get_auth_creds(auth_attr)
        try:
            core_v1_api_client = self.kubernetes.get_core_v1_api_client(
                auth=auth_cred)
            app_v1_api_client = self.kubernetes.get_app_v1_api_client(
                auth=auth_cred)
            scaling_api_client = self.kubernetes.get_scaling_api_client(
                auth=auth_cred)

            deployment_names = vnf_id.split(COMMA_CHARACTER)
            keep_going = True
            stack_retries = self.STACK_RETRIES
            while keep_going and stack_retries > 0:
                count = 0
                for i in range(0, len(deployment_names), 2):
                    namespace = deployment_names[i]
                    deployment_name = deployment_names[i + 1]
                    try:
                        core_v1_api_client.read_namespaced_config_map(
                            namespace=namespace,
                            name=deployment_name)
                        count = count + 1
                    except Exception:
                        pass
                    try:
                        core_v1_api_client.read_namespaced_service(
                            namespace=namespace,
                            name=deployment_name)
                        count = count + 1
                    except Exception:
                        pass
                    try:
                        scaling_api_client.\
                            read_namespaced_horizontal_pod_autoscaler(
                                namespace=namespace,
                                name=deployment_name)
                        count = count + 1
                    except Exception:
                        pass
                    try:
                        app_v1_api_client.read_namespaced_deployment(
                            namespace=namespace,
                            name=deployment_name)
                        count = count + 1
                    except Exception:
                        pass
                stack_retries = stack_retries - 1
                # If one of objects is still alive, keeps on waiting
                if count > 0:
                    keep_going = True
                else:
                    keep_going = False
        except Exception as e:
            LOG.error('Deleting wait VNF got an error due to %s', e)
            raise
        finally:
            self.clean_authenticate_vim(auth_cred, file_descriptor)

    @log.log
    def scale(self, context, plugin, auth_attr, policy, region_name):
        """Scale function

        Scaling VNF is implemented by updating replicas through Kubernetes API.
        The min_replicas and max_replicas is limited by the number of replicas
        of policy scaling when user define VNF descriptor.
        """
        LOG.debug("VNF are scaled by updating instance of deployment")
        # initialize Kubernetes APIs
        auth_cred, file_descriptor = self._get_auth_creds(auth_attr)
        try:
            app_v1_api_client = self.kubernetes.get_app_v1_api_client(
                auth=auth_cred)
            scaling_api_client = self.kubernetes.get_scaling_api_client(
                auth=auth_cred)
            deployment_names = policy['instance_id'].split(COMMA_CHARACTER)
            policy_name = policy['name']
            policy_action = policy['action']

            for i in range(0, len(deployment_names), 2):
                namespace = deployment_names[i]
                deployment_name = deployment_names[i + 1]
                deployment_info = app_v1_api_client.\
                    read_namespaced_deployment(namespace=namespace,
                                               name=deployment_name)
                scaling_info = scaling_api_client.\
                    read_namespaced_horizontal_pod_autoscaler(
                        namespace=namespace,
                        name=deployment_name)

                replicas = deployment_info.status.replicas
                scale_replicas = replicas
                vnf_scaling_name = deployment_info.metadata.labels.\
                    get("scaling_name")
                if vnf_scaling_name == policy_name:
                    if policy_action == 'out':
                        scale_replicas = replicas + 1
                    elif policy_action == 'in':
                        scale_replicas = replicas - 1

                min_replicas = scaling_info.spec.min_replicas
                max_replicas = scaling_info.spec.max_replicas
                if (scale_replicas < min_replicas) or \
                        (scale_replicas > max_replicas):
                    LOG.debug("Scaling replicas is out of range. The number of"
                              " replicas keeps %(number)s replicas",
                              {'number': replicas})
                    scale_replicas = replicas
                deployment_info.spec.replicas = scale_replicas
                app_v1_api_client.patch_namespaced_deployment_scale(
                    namespace=namespace,
                    name=deployment_name,
                    body=deployment_info)
        except Exception as e:
            LOG.error('Scaling VNF got an error due to %s', e)
            raise
        finally:
            self.clean_authenticate_vim(auth_cred, file_descriptor)

    def scale_wait(self, context, plugin, auth_attr, policy, region_name,
                   last_event_id):
        """Scale wait function

        Scale wait function will marked VNF is ACTIVE when all status state
        from Pod objects is RUNNING.
        """
        # initialize Kubernetes APIs
        auth_cred, file_descriptor = self._get_auth_creds(auth_attr)
        try:
            core_v1_api_client = self.kubernetes.get_core_v1_api_client(
                auth=auth_cred)
            deployment_info = policy['instance_id'].split(",")

            pods_information = self._get_pods_information(
                core_v1_api_client=core_v1_api_client,
                deployment_info=deployment_info)
            status = self._get_pod_status(pods_information)

            stack_retries = self.STACK_RETRIES
            error_reason = None
            while status == 'Pending' and stack_retries > 0:
                time.sleep(self.STACK_RETRY_WAIT)

                pods_information = self._get_pods_information(
                    core_v1_api_client=core_v1_api_client,
                    deployment_info=deployment_info)
                status = self._get_pod_status(pods_information)

                # LOG.debug('status: %s', status)
                stack_retries = stack_retries - 1

            LOG.debug('VNF initializing status: %(service_name)s %(status)s',
                      {'service_name': str(deployment_info), 'status': status})

            if stack_retries == 0 and status != 'Running':
                error_reason = _("Resource creation is not completed within"
                                 " {wait} seconds as creation of stack {stack}"
                                 " is not completed").format(
                    wait=(self.STACK_RETRIES *
                          self.STACK_RETRY_WAIT),
                    stack=policy['instance_id'])
                LOG.error("VNF Creation failed: %(reason)s",
                          {'reason': error_reason})
                raise vnfm.VNFCreateWaitFailed(reason=error_reason)

            elif stack_retries != 0 and status != 'Running':
                raise vnfm.VNFCreateWaitFailed(reason=error_reason)
        except Exception as e:
            LOG.error('Scaling wait VNF got an error due to %s', e)
            raise
        finally:
            self.clean_authenticate_vim(auth_cred, file_descriptor)

    @log.log
    def get_resource_info(self, plugin, context, vnf_info, auth_attr,
                          region_name=None):
        # TODO(phuoc): will update it for other components
        pass

    def _get_auth_creds(self, auth_cred):
        file_descriptor = self._create_ssl_ca_file(auth_cred)
        if ('username' not in auth_cred) and ('password' not in auth_cred):
            auth_cred['username'] = 'None'
            auth_cred['password'] = None
        return auth_cred, file_descriptor

    def _create_ssl_ca_file(self, auth_attr):
        ca_cert = utils.none_from_string(auth_attr.get('ssl_ca_cert'))
        if ca_cert is not None:
            file_descriptor, file_path = \
                self.kubernetes.create_ca_cert_tmp_file(ca_cert)
            auth_attr['ca_cert_file'] = file_path
            return file_descriptor
        else:
            return None

    def clean_authenticate_vim(self, vim_auth, file_descriptor):
        # remove ca_cert_file from vim_obj if it exists
        # close and delete temp ca_cert_file
        if file_descriptor is not None:
            file_path = vim_auth.pop('ca_cert_file')
            self.kubernetes.close_tmp_file(file_descriptor, file_path)

    def heal_vdu(self, plugin, context, vnf_dict, heal_request_data):
        pass

    def get_artifact_files(self, instantiate_vnf_req):
        if instantiate_vnf_req.additional_params and\
                'lcm-kubernetes-def-files' in\
                instantiate_vnf_req.additional_params.keys():
            artifact_files = instantiate_vnf_req.\
                additional_params['lcm-kubernetes-def-files']
		else:
            artifact_files = list()
        return artifact_files

    def pre_instantiation_vnf(self, context, vnf_instance,
                              vim_connection_info, vnf_software_images,
                              instantiate_vnf_req, vnf_package_path):
        vnf_resources = dict()
        artifact_files = self.get_artifact_files(instantiate_vnf_req)
        if artifact_files:
            vnfd = vnfd_obj.VnfPackageVnfd.get_by_id(
                context, vnf_instance.vnfd_id)
            package_uuid = vnfd.package_uuid
            vnf_package = vnf_package_obj.VnfPackage.get_by_id(
                context, package_uuid, expected_attrs=['vnf_artifacts'])
            vnf_artifacts = vnf_package.vnf_artifacts
            length = len(vnf_artifacts)
            if length:
                for artifact_file in artifact_files:
                    for index, vnf_artifact in enumerate(vnf_artifacts):
                        if vnf_artifact.artifact_path == artifact_file:
                            break
                        if length > 1 and index < length - 1:
                            continue
                        raise vnfm.CnfDefinitionNotFound(
                            path=artifact_file)
            for artifact_file in artifact_files:
                if ((urlparse(artifact_file).scheme == 'file') or
                        (bool(urlparse(artifact_file).scheme) and
                         bool(urlparse(artifact_file).netloc))):
                    file_content = urllib2.urlopen(artifact_file).read()
                else:
                    artifact_file_path = os.path.join(
                        vnf_package_path, artifact_file)
                    with open(artifact_file_path, 'r') as f:
                        file_content = f.read()
                file_content_dict_list = yaml.safe_load_all(file_content)
                for inx, file_content_dict in enumerate(
                        file_content_dict_list):
                    vnf_resource = vnf_resource_obj.VnfResource(
                        context=context)
                    vnf_resource['vnf_instance_id'] = vnf_instance.id
                    vnf_resource['resource_name'] = ','.join([
                        file_content_dict.get('metadata').get('namespace'),
                        file_content_dict.get('metadata').get('name')])
                    vnf_resource['resource_type'] = ','.join([
                        file_content_dict.get('apiVersion'),
                        file_content_dict.get('kind')])
                    vnf_resources[inx] = vnf_resource
            return vnf_resources

    def delete_vnf_instance_resource(self, context, vnf_instance,
            vim_connection_info, vnf_resource):
        raise NotImplementedError()

    def instantiate_vnf(self, context, vnf_instance, vnfd_dict,
                        vim_connection_info, instantiate_vnf_req,
                        grant_response, vnf_package_path, base_hot_dict):
        artifact_files = self.get_artifact_files(instantiate_vnf_req)
        auth_attr = vim_connection_info.access_info
        if not artifact_files:
            instance_id = self.create(
                None, context, vnf_instance, auth_attr)
            return instance_id
        else:
            auth_cred, file_descriptor = self._get_auth_creds(auth_attr)
            k8s_client_dict = self.kubernetes.get_k8s_client_dict(auth_cred)
            transformer = translate_outputs.Transformer(k8s_client_dict)
            deployment_dict_list = list()
            k8s_objs = transformer.\
                get_k8s_objs_from_yaml(artifact_files, vnf_package_path)
            k8s_objs = transformer.deploy(k8s_objs)
            k8s_objs = self.create_wait_new(k8s_objs, k8s_client_dict)
            for k8s_obj in k8s_objs:
                deployment_dict = dict()
                deployment_dict['namespace'] = k8s_obj.get('namespace')
                deployment_dict['name'] = k8s_obj.get('objects').metadata.name
                deployment_dict['apiVersion'] = k8s_obj.get(
                    'objects').apiVersion
                deployment_dict['kind'] = k8s_obj.get('objects').kind
                deployment_dict['status'] = k8s_obj.get('status')
                deployment_dict_list.append(deployment_dict)
            deployment_str_list = [str(x) for x in deployment_dict_list]
            resource_info_str = ';'.join(deployment_str_list)
            self.clean_authenticate_vim(auth_cred, file_descriptor)
            return resource_info_str

    def post_vnf_instantiation(self, context, vnf_instance,
                               vim_connection_info):
        pass

    def heal_vnf(self, context, vnf_instance, vim_connection_info,
                 heal_vnf_request):
        raise NotImplementedError()

    def heal_vnf_wait(self, context, vnf_instance, vim_connection_info):
        raise NotImplementedError()

    def post_heal_vnf(self, context, vnf_instance, vim_connection_info,
                      heal_vnf_request):
        raise NotImplementedError()
