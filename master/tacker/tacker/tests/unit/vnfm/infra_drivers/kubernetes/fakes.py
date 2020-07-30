# Copyright (C) 2020 NTT DATA
# All Rights Reserved.
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


from kubernetes import client

CREATE_K8S_FALSE_VALUE = None


def fake_k8s_dict():
    k8s_client_dict = {
        'namespace': 'curryns',
        'object': fake_k8s_obj()
    }
    return k8s_client_dict


def fake_k8s_obj():
    return client.V1Deployment(
        api_version='apps/v1',
        kind='Deployment',
        metadata=client.V1ObjectMeta(
            name='curry-test001',
            namespace='curryns'
        ),
        spec=client.V1DeploymentSpec(
            replicas=2,
            selector=client.V1LabelSelector(
                match_labels={'app': 'webserver'}
            ),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(
                    labels={'app': 'webserver',
                            'scaling_name': 'SP1'}
                ),
                spec=client.V1PodSpec(
                    containers=[
                        client.V1Container(
                            env=[
                                client.V1EnvVar(
                                    name='param0',
                                    value_from=client.V1EnvVarSource(
                                        config_map_key_ref=client.
                                        V1ConfigMapKeySelector(
                                            key='param0',
                                            name='curry-test001'
                                        )
                                    )
                                ),
                                client.V1EnvVar(
                                    name='param1',
                                    value_from=
                                    client.V1EnvVarSource(
                                        config_map_key_ref=client.
                                        V1ConfigMapKeySelector(
                                            key='param1',
                                            name='curry-test001'
                                        )
                                    )
                                )
                            ],
                            image='celebdor/kuryr-demo',
                            image_pull_policy='IfNotPresent',
                            name='web-server',
                            ports=[
                                client.V1ContainerPort(
                                    container_port=8080
                                )
                            ],
                            resources=client.V1ResourceRequirements(
                                limits={
                                    'cpu': '500m', 'memory': '512M'
                                },
                                requests={
                                    'cpu': '500m', 'memory': '512M'
                                }
                            ),
                            volume_mounts=[
                                client.V1VolumeMount(
                                    name='curry-claim-volume',
                                    mount_path='/data'
                                )
                            ]
                        )
                    ],
                    volumes=[
                        client.V1Volume(
                            name='curry-claim-volume',
                            persistent_volume_claim=client.
                            V1PersistentVolumeClaimVolumeSource(
                                claim_name='curry-pv-claim'
                            )
                        )
                    ],
                    termination_grace_period_seconds=0
                )
            )
        )
    )


def fake_k8s_client_dict():
    client_value = client.AppsV1Api()
    k8s_client_dict = {
        'apps/v1': client_value
    }
    return k8s_client_dict
