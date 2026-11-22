from pydantic import SecretStr

from danmaku.application.credential_changes import CredentialChanges, credential_changes
from danmaku.application.ports import CredentialStore, Repository, UnitOfWork
from danmaku.domain.analysis import MOCK_CONFIG, ModelConfig, ModelConfigInput, ModelSettings
from danmaku.domain.workbench import Principal, WorkbenchError


def require_config(repository: Repository, owner_id: str, version: int) -> ModelConfig:
    config = MOCK_CONFIG if version == 0 else repository.model_config(owner_id, version)
    if config is None:
        raise WorkbenchError(404, "llm_config_not_found", "找不到模型配置版本")
    return config


class Settings:
    def __init__(self, database: UnitOfWork, credentials: CredentialStore):
        self.database = database
        self.credentials = credentials

    def _secret(self, repository: Repository, owner_id: str, config: ModelConfig) -> SecretStr | None:
        if config.base_url is None:
            return None
        reference = repository.model_credential(owner_id, config.base_url)
        return self.credentials.get(reference) if reference else None

    def secret(self, owner_id: str, config: ModelConfig) -> SecretStr | None:
        if config.provider == "mock":
            return None
        with self.database() as repository:
            secret = self._secret(repository, owner_id, config)
        if secret is None:
            raise WorkbenchError(409, "llm_credential_missing", "请为该模型端点保存 API 密钥")
        return secret

    def get(self, actor: Principal) -> ModelSettings:
        with self.database() as repository:
            return self.view(repository, actor.id)

    def view(self, repository: Repository, owner_id: str) -> ModelSettings:
        config = repository.model_config(owner_id) or MOCK_CONFIG
        configured = config.provider == "mock" or self._secret(repository, owner_id, config) is not None
        return ModelSettings(**config.model_dump(), configured=configured)

    def put(self, actor: Principal, command: ModelConfigInput) -> ModelSettings:
        with credential_changes() as changes, self.database(write=True) as repository:
            return self.save(repository, actor.id, command, changes)

    def save(
        self, repository: Repository, owner_id: str, command: ModelConfigInput, changes: CredentialChanges,
    ) -> ModelSettings:
        endpoint = command.base_url
        if (command.provider == "mock" and (endpoint or command.api_key is not None or command.remove_api_key)) or (
            command.provider == "openai_compatible" and endpoint is None
        ) or (command.remove_api_key and command.api_key is not None):
            raise WorkbenchError(422, "invalid_llm_config", "模型服务商、端点或密钥参数不匹配")

        latest = repository.model_config(owner_id) or MOCK_CONFIG
        config = ModelConfig(
            provider=command.provider, base_url=endpoint, model=command.model, version=latest.version + 1,
            thinking_enabled=command.thinking_enabled, reasoning_effort=command.reasoning_effort,
            extra_requirements=command.extra_requirements,
        )
        if command.api_key is not None:
            assert endpoint is not None
            previous = repository.model_credential(owner_id, endpoint)
            reference = changes.put(self.credentials, command.api_key, previous)
            repository.save_model_credential(owner_id, endpoint, reference)
            configured = True
        elif command.remove_api_key:
            assert endpoint is not None
            changes.remove(self.credentials, repository.model_credential(owner_id, endpoint))
            repository.remove_model_credential(owner_id, endpoint)
            configured = False
        else:
            configured = config.provider == "mock" or self._secret(repository, owner_id, config) is not None

        repository.save_model_config(owner_id, config)
        return ModelSettings(**config.model_dump(), configured=configured)
