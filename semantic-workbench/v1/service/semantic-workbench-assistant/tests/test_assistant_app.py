import datetime
import io
import pathlib
import random
import tempfile
import uuid
from contextlib import asynccontextmanager
from typing import IO, AsyncIterator
from unittest import mock

import httpx
import pytest
import semantic_workbench_api_model
import semantic_workbench_api_model.assistant_service_client
from asgi_lifespan import LifespanManager
from fastapi import HTTPException
from pydantic import BaseModel
from semantic_workbench_api_model import (
    assistant_model,
    assistant_service_client,
    workbench_model,
    workbench_service_client,
)
from semantic_workbench_assistant import settings, storage
from semantic_workbench_assistant.assistant_app import (
    AssistantApp,
    AssistantContext,
    AssistantConversationInspectorStateDataModel,
    BadRequestError,
    BaseModelAssistantConfigWithSecrets,
    ConflictError,
    ConversationContext,
    FileStorageContext,
    FileStorageConversationDataExporter,
    NotFoundError,
)
from semantic_workbench_assistant.assistant_app.service import (
    translate_assistant_errors,
)
from semantic_workbench_assistant.config import (
    ConfigSecretStr,
)


class AllOKTransport(httpx.AsyncBaseTransport):
    """
    A mock transport that always returns a 200 OK response.
    """

    async def handle_async_request(self, request) -> httpx.Response:
        return httpx.Response(200)


async def test_assistant_with_event_handlers(
    monkeypatch: pytest.MonkeyPatch, storage_settings: storage.FileStorageSettings
) -> None:
    monkeypatch.setattr(settings, "storage", storage_settings)

    app = AssistantApp(
        assistant_service_id="assistant_id",
        assistant_service_name="service name",
        assistant_service_description="service description",
    )

    assistant_created_calls = 0
    conversation_created_calls = 0
    message_created_calls = 0
    message_chat_created_calls = 0

    @app.events.assistant.on_created
    async def on_assistant_created(assistant_context: AssistantContext) -> None:
        nonlocal assistant_created_calls
        assistant_created_calls += 1

    @app.events.conversation.on_created
    async def on_conversation_created(conversation_context: ConversationContext) -> None:
        nonlocal conversation_created_calls
        conversation_created_calls += 1

    @app.events.conversation.message.on_created
    def on_message_created(
        conversation_context: ConversationContext,
        _: workbench_model.ConversationEvent,
        message: workbench_model.ConversationMessage,
    ) -> None:
        nonlocal message_created_calls
        message_created_calls += 1

    @app.events.conversation.message.chat.on_created
    async def on_chat_message(
        conversation_context: ConversationContext,
        _: workbench_model.ConversationEvent,
        message: workbench_model.ConversationMessage,
    ) -> None:
        nonlocal message_chat_created_calls
        message_chat_created_calls += 1

    service = app.fastapi_app()

    monkeypatch.setattr(assistant_service_client, "httpx_transport", httpx.ASGITransport(app=service))
    monkeypatch.setattr(workbench_service_client, "httpx_transport", AllOKTransport())

    async with LifespanManager(service):
        assistant_id = uuid.uuid4()
        assistant_request = assistant_model.AssistantPutRequestModel(assistant_name="my assistant")

        client_builder = assistant_service_client.AssistantServiceClientBuilder("https://fake", "")
        service_client = client_builder.for_service()
        instance_client = client_builder.for_assistant_instance(assistant_id)

        await service_client.put_assistant_instance(
            assistant_id=assistant_id, request=assistant_request, from_export=None
        )

        assert assistant_created_calls == 1

        conversation_id = uuid.uuid4()

        await instance_client.put_conversation(
            request=assistant_model.ConversationPutRequestModel(
                id=str(conversation_id),
                title="My conversation",
            ),
            from_export=None,
        )

        assert conversation_created_calls == 1

        # send a message of type "chat"
        message_id = uuid.uuid4()
        await instance_client.post_conversation_event(
            event=workbench_model.ConversationEvent(
                conversation_id=conversation_id,
                correlation_id="",
                event=workbench_model.ConversationEventType.message_created,
                data={
                    "message": workbench_model.ConversationMessage(
                        id=message_id,
                        sender=workbench_model.MessageSender(
                            participant_role=workbench_model.ParticipantRole.user, participant_id="user"
                        ),
                        message_type=workbench_model.MessageType.chat,
                        timestamp=datetime.datetime.now(),
                        content_type="text/plain",
                        content="Hello, world",
                        filenames=[],
                        metadata={},
                    ).model_dump(mode="json")
                },
            )
        )

        assert message_created_calls == 1
        assert message_chat_created_calls == 1

        # send a message of type "notice"
        await instance_client.post_conversation_event(
            event=workbench_model.ConversationEvent(
                conversation_id=conversation_id,
                correlation_id="",
                event=workbench_model.ConversationEventType.message_created,
                data={
                    "message": workbench_model.ConversationMessage(
                        id=message_id,
                        sender=workbench_model.MessageSender(
                            participant_role=workbench_model.ParticipantRole.user, participant_id="user"
                        ),
                        message_type=workbench_model.MessageType.notice,
                        timestamp=datetime.datetime.now(),
                        content_type="text/plain",
                        content="Hello, world",
                        filenames=[],
                        metadata={},
                    ).model_dump(mode="json")
                },
            )
        )

        assert message_created_calls == 2
        assert message_chat_created_calls == 1


async def test_assistant_with_inspector(
    monkeypatch: pytest.MonkeyPatch, storage_settings: storage.FileStorageSettings
) -> None:
    monkeypatch.setattr(settings, "storage", storage_settings)

    class TestInspectorImplementation:
        display_name = "Test"
        description = "Test inspector"

        async def get(self, context: ConversationContext) -> AssistantConversationInspectorStateDataModel:
            return AssistantConversationInspectorStateDataModel(
                data={"test": "data"},
                json_schema={},
                ui_schema={},
            )

    app = AssistantApp(
        assistant_service_id="assistant_id",
        assistant_service_name="service name",
        assistant_service_description="service description",
        inspector_state_providers={"test": TestInspectorImplementation()},
    )

    service = app.fastapi_app()

    monkeypatch.setattr(assistant_service_client, "httpx_transport", httpx.ASGITransport(app=service))
    monkeypatch.setattr(workbench_service_client, "httpx_transport", AllOKTransport())

    async with LifespanManager(service):
        assistant_id = uuid.uuid4()
        conversation_id = uuid.uuid4()

        assistant_request = assistant_model.AssistantPutRequestModel(assistant_name="my assistant")

        client_builder = assistant_service_client.AssistantServiceClientBuilder("https://fake", "")
        service_client = client_builder.for_service()
        instance_client = client_builder.for_assistant_instance(assistant_id)

        await service_client.put_assistant_instance(
            assistant_id=assistant_id, request=assistant_request, from_export=None
        )
        await instance_client.put_conversation(
            request=assistant_model.ConversationPutRequestModel(
                id=str(conversation_id),
                title="My conversation",
            ),
            from_export=None,
        )

        response = await instance_client.get_state_descriptions(conversation_id=conversation_id)
        assert response == assistant_model.StateDescriptionListResponseModel(
            states=[
                assistant_model.StateDescriptionResponseModel(
                    id="test",
                    display_name="Test",
                    description="Test inspector",
                )
            ]
        )

        response = await instance_client.get_state(conversation_id=conversation_id, state_id="test")
        assert response == assistant_model.StateResponseModel(
            id="test",
            data={"test": "data"},
            json_schema={},
            ui_schema={},
        )


async def test_assistant_with_state_exporter(
    monkeypatch: pytest.MonkeyPatch, storage_settings: storage.FileStorageSettings
) -> None:
    monkeypatch.setattr(settings, "storage", storage_settings)

    class SimpleStateExporter:
        def __init__(self) -> None:
            self.data = bytearray()

        @asynccontextmanager
        async def export(self, conversation_context: ConversationContext) -> AsyncIterator[IO[bytes]]:
            yield io.BytesIO(self.data)

        async def import_(self, conversation_context: ConversationContext, stream: IO[bytes]) -> None:
            self.data = stream.read()

    state_exporter = SimpleStateExporter()
    # wrap the instance so we can check calls to it
    state_exporter_wrapper = mock.Mock(wraps=state_exporter)

    app = AssistantApp(
        assistant_service_id="assistant_id",
        assistant_service_name="service name",
        assistant_service_description="service description",
        conversation_data_exporter=state_exporter_wrapper,
    )

    service = app.fastapi_app()

    monkeypatch.setattr(assistant_service_client, "httpx_transport", httpx.ASGITransport(app=service))
    monkeypatch.setattr(workbench_service_client, "httpx_transport", AllOKTransport())

    async with LifespanManager(service):
        assistant_id = uuid.uuid4()
        assistant_request = assistant_model.AssistantPutRequestModel(assistant_name="my assistant")

        client_builder = assistant_service_client.AssistantServiceClientBuilder("https://fake", "")
        service_client = client_builder.for_service()
        instance_client = client_builder.for_assistant_instance(assistant_id)

        await service_client.put_assistant_instance(
            assistant_id=assistant_id, request=assistant_request, from_export=None
        )

        conversation_id = uuid.uuid4()

        import_bytes = bytearray(random.getrandbits(8) for _ in range(10))

        await instance_client.put_conversation(
            request=assistant_model.ConversationPutRequestModel(
                id=str(conversation_id),
                title="My conversation",
            ),
            from_export=io.BytesIO(import_bytes),
        )

        assert state_exporter_wrapper.import_.called
        assert state_exporter_wrapper.import_.call_args[0][0] == ConversationContext(
            id=str(conversation_id),
            title="My conversation",
            assistant=mock.ANY,
        )

        assert state_exporter.data == import_bytes

        bytes_out = bytearray()
        async with instance_client.get_exported_conversation_data(conversation_id=conversation_id) as stream:
            async for chunk in stream:
                bytes_out.extend(chunk)

        assert state_exporter_wrapper.export.called
        assert state_exporter_wrapper.export.call_args[0][0] == ConversationContext(
            id=str(conversation_id),
            title="My conversation",
            assistant=mock.ANY,
        )

        assert bytes_out == import_bytes


async def test_assistant_with_config_provider(
    monkeypatch: pytest.MonkeyPatch, storage_settings: storage.FileStorageSettings
) -> None:
    monkeypatch.setattr(settings, "storage", storage_settings)

    class TestConfigModel(BaseModel):
        test_key: str = "test_value"

    class TestConfigSecretModel(BaseModel):
        secret_field: ConfigSecretStr = ""

    config_provider = BaseModelAssistantConfigWithSecrets(TestConfigModel(), TestConfigSecretModel()).provider
    # wrap the provider so we can check calls to it
    config_provider_wrapper = mock.Mock(wraps=config_provider)

    expected_json_schema = {
        "$defs": {
            "ConfigSecretStr": {"format": "password", "type": "string", "writeOnly": True},
            "TestConfigModel": {
                "properties": {"test_key": {"default": "test_value", "title": "Test Key", "type": "string"}},
                "title": "TestConfigModel",
                "type": "object",
            },
            "TestConfigSecretModel": {
                "properties": {"secret_field": {"$ref": "#/$defs/ConfigSecretStr", "default": ""}},
                "title": "TestConfigSecretModel",
                "type": "object",
            },
        },
        "properties": {
            "config": {"$ref": "#/$defs/TestConfigModel"},
            "config_secrets": {"$ref": "#/$defs/TestConfigSecretModel"},
        },
        "required": ["config", "config_secrets"],
        "title": "CombinedConfigModel",
        "type": "object",
    }

    expected_ui_schema = {
        "config": {"ui:options": {"hide_title": True}},
        "config_secrets": {"ui:options": {"hide_title": True}, "secret_field": {"ui:options": {"widget": "password"}}},
    }

    app = AssistantApp(
        assistant_service_id="assistant_id",
        assistant_service_name="service name",
        assistant_service_description="service description",
        config_provider=config_provider_wrapper,
    )

    service = app.fastapi_app()

    monkeypatch.setattr(assistant_service_client, "httpx_transport", httpx.ASGITransport(app=service))
    monkeypatch.setattr(workbench_service_client, "httpx_transport", AllOKTransport())

    async with LifespanManager(service):
        assistant_id = uuid.uuid4()
        assistant_request = assistant_model.AssistantPutRequestModel(assistant_name="my assistant")

        client_builder = assistant_service_client.AssistantServiceClientBuilder("https://fake", "")
        service_client = client_builder.for_service()
        instance_client = client_builder.for_assistant_instance(assistant_id)

        await service_client.put_assistant_instance(
            assistant_id=assistant_id, request=assistant_request, from_export=None
        )

        response = await instance_client.get_config()
        assert response == assistant_model.ConfigResponseModel(
            config={"config": {"test_key": "test_value"}, "config_secrets": {"secret_field": ""}},
            json_schema=expected_json_schema,
            ui_schema=expected_ui_schema,
        )
        assert config_provider_wrapper.get.called

        config_provider_wrapper.reset_mock()

        response = await instance_client.put_config(
            assistant_model.ConfigPutRequestModel(
                config={"config": {"test_key": "new_value"}, "config_secrets": {"secret_field": "new_secret"}}
            )
        )
        assert response == assistant_model.ConfigResponseModel(
            config={"config": {"test_key": "new_value"}, "config_secrets": {"secret_field": "**********"}},
            json_schema=expected_json_schema,
            ui_schema=expected_ui_schema,
        )
        assert config_provider_wrapper.set.called
        assert config_provider_wrapper.set.call_args[0][1] == {
            "config": {"test_key": "new_value"},
            "config_secrets": {"secret_field": "new_secret"},
        }

        config_provider_wrapper.reset_mock()

        response = await instance_client.get_config()
        assert response == assistant_model.ConfigResponseModel(
            config={"config": {"test_key": "new_value"}, "config_secrets": {"secret_field": "**********"}},
            json_schema=expected_json_schema,
            ui_schema=expected_ui_schema,
        )
        assert config_provider_wrapper.get.called

        with pytest.raises(semantic_workbench_api_model.assistant_service_client.AssistantResponseError) as e:
            await instance_client.put_config(
                assistant_model.ConfigPutRequestModel(config={"config": {"test_key": {"invalid_value": 1}}})
            )

        assert e.value.status_code == 400


async def test_file_system_storage_state_data_provider_to_empty_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as src_temp_dir, tempfile.TemporaryDirectory() as dest_temp_dir:
        src_dir_path = pathlib.Path(src_temp_dir)

        (src_dir_path / "test.txt").write_text("Hello, world")

        sub_dir_path = src_dir_path / "subdir"

        sub_dir_path.mkdir()

        (sub_dir_path / "test.bin").write_bytes(bytes([1, 2, 3, 4]))

        src_conversation_context = ConversationContext(
            id=str(uuid.uuid4()),
            title="My conversation",
            assistant=AssistantContext(
                _assistant_service_id="",
                id=str(uuid.uuid4()),
                name="my assistant",
            ),
        )

        dest_dir_path = pathlib.Path(dest_temp_dir)

        dest_conversation_context = ConversationContext(
            id=str(uuid.uuid4()),
            title="My conversation",
            assistant=AssistantContext(
                _assistant_service_id="",
                id=str(uuid.uuid4()),
                name="my assistant",
            ),
        )

        def file_storage_context_get_mock(conversation_context: ConversationContext) -> FileStorageContext:
            if conversation_context == src_conversation_context:
                return FileStorageContext(directory=src_dir_path)
            return FileStorageContext(directory=dest_dir_path)

        with mock.patch(
            "semantic_workbench_assistant.assistant_app.FileStorageContext.get",
            side_effect=file_storage_context_get_mock,
        ):
            data_provider = FileStorageConversationDataExporter()

            async with data_provider.export(src_conversation_context) as stream:
                await data_provider.import_(dest_conversation_context, stream)

            assert (dest_dir_path / "test.txt").read_text() == "Hello, world"

            assert (dest_dir_path / "subdir" / "test.bin").read_bytes() == bytes([1, 2, 3, 4])


async def test_file_system_storage_state_data_provider_to_non_empty_dir():
    with tempfile.TemporaryDirectory() as src_temp_dir, tempfile.TemporaryDirectory() as dest_temp_dir:
        # set up contents of the non-empty destination directory
        dest_dir_path = pathlib.Path(dest_temp_dir)

        (dest_dir_path / "test.txt").write_text("this file will be overwritten")

        dest_sub_dir_path = dest_dir_path / "subdir-gets-deleted"

        dest_sub_dir_path.mkdir()

        (dest_sub_dir_path / "test.bin").write_bytes(bytes([1, 2, 3, 4]))

        # set up contents of the source directory

        src_dir_path = pathlib.Path(src_temp_dir)

        (src_dir_path / "test.txt").write_text("Hello, world")

        sub_dir_path = src_dir_path / "subdir"

        sub_dir_path.mkdir()

        (sub_dir_path / "test.bin").write_bytes(bytes([1, 2, 3, 4]))

        # export and import

        src_conversation_context = ConversationContext(
            id=str(uuid.uuid4()),
            title="My conversation",
            assistant=AssistantContext(
                _assistant_service_id="",
                id=str(uuid.uuid4()),
                name="my assistant",
            ),
        )

        dest_conversation_context = ConversationContext(
            id=str(uuid.uuid4()),
            title="My conversation",
            assistant=AssistantContext(
                _assistant_service_id="",
                id=str(uuid.uuid4()),
                name="my assistant",
            ),
        )

        def file_storage_context_get_mock(conversation_context: ConversationContext) -> FileStorageContext:
            if conversation_context == src_conversation_context:
                return FileStorageContext(directory=src_dir_path)
            return FileStorageContext(directory=dest_dir_path)

        with mock.patch(
            "semantic_workbench_assistant.assistant_app.FileStorageContext.get",
            side_effect=file_storage_context_get_mock,
        ):
            data_provider = FileStorageConversationDataExporter()

            async with data_provider.export(src_conversation_context) as stream:
                await data_provider.import_(dest_conversation_context, stream)

            assert (dest_dir_path / "test.txt").read_text() == "Hello, world"

            assert (dest_dir_path / "subdir" / "test.bin").read_bytes() == bytes([1, 2, 3, 4])

            assert dest_sub_dir_path.exists() is False


class UnknownErrorForTest(Exception):
    pass


@pytest.mark.parametrize(
    "raise_exception,expected_exception,expected_status_code",
    [
        [UnknownErrorForTest(), UnknownErrorForTest, None],
        (NotFoundError(), HTTPException, 404),
        (ConflictError(), HTTPException, 409),
        (BadRequestError(), HTTPException, 400),
    ],
)
async def test_translate_assistant_errors(
    raise_exception: Exception, expected_exception: type[Exception], expected_status_code: int | None
) -> None:
    @translate_assistant_errors
    def raise_err_sync() -> None:
        raise raise_exception

    @translate_assistant_errors
    async def raise_err_async() -> None:
        raise raise_exception

    with pytest.raises(expected_exception) as exc_info:
        raise_err_sync()

    if isinstance(exc_info.value, HTTPException):
        assert exc_info.value.status_code == expected_status_code

    with pytest.raises(expected_exception) as exc_info:
        await raise_err_async()

    if isinstance(exc_info.value, HTTPException):
        assert exc_info.value.status_code == expected_status_code
