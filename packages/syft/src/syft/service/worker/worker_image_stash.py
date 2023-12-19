# stdlib
from typing import List
from typing import Union

# third party
from result import Err
from result import Result

# relative
from ...custom_worker.config import DockerWorkerConfig
from ...node.credentials import SyftVerifyKey
from ...serde.serializable import serializable
from ...store.document_store import BaseUIDStoreStash
from ...store.document_store import DocumentStore
from ...store.document_store import PartitionKey
from ...store.document_store import PartitionSettings
from ...store.document_store import QueryKeys
from ..action.action_permissions import ActionObjectPermission
from .worker_image import SyftWorkerImage

DockerWorkerConfigPK = PartitionKey(key="config", type_=DockerWorkerConfig)


@serializable()
class SyftWorkerImageStash(BaseUIDStoreStash):
    object_type = SyftWorkerImage
    settings: PartitionSettings = PartitionSettings(
        name=SyftWorkerImage.__canonical_name__,
        object_type=SyftWorkerImage,
    )

    def __init__(self, store: DocumentStore) -> None:
        super().__init__(store=store)

    def set(
        self,
        credentials: SyftVerifyKey,
        obj: SyftWorkerImage,
        add_permissions: Union[List[ActionObjectPermission], None] = None,
        ignore_duplicates: bool = False,
    ) -> Result[SyftWorkerImage, str]:
        if isinstance(obj.config, DockerWorkerConfig):
            result = self.get_by_docker_config(
                credentials=credentials, config=obj.config
            )
            if result.is_ok() and result.ok() is not None:
                return Err(f"Image already exists for given DockerConfig: {obj.config}")

        return super().set(credentials, obj, add_permissions, ignore_duplicates)

    def get_by_docker_config(
        self, credentials: SyftVerifyKey, config: DockerWorkerConfig
    ):
        qks = QueryKeys(qks=[DockerWorkerConfigPK.from_obj(config)])
        return self.query_one(credentials=credentials, qks=qks)
