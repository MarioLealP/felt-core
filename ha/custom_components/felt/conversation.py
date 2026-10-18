"""The conversation agent: what Mario says to Assist goes to Felt, her reply comes back."""

from __future__ import annotations

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .client import FeltClient, FeltError
from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([FeltAgent(entry)])


class FeltAgent(conversation.ConversationEntity):
    """Felt answers - her own brain, skills and memory in felt-core; HA only
    carries the words (she does the device control herself)."""

    _attr_has_entity_name = True
    _attr_name = None

    def __init__(self, entry: ConfigEntry) -> None:
        self._client: FeltClient = entry.runtime_data.client
        self._attr_unique_id = entry.entry_id
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Felt",
            "manufacturer": "clockmost",
        }

    @property
    def supported_languages(self) -> str:
        return MATCH_ALL

    async def _async_handle_message(
        self, user_input: conversation.ConversationInput, chat_log: conversation.ChatLog
    ) -> conversation.ConversationResult:
        try:
            reply = await self._client.chat(user_input.text, user_input.language)
        except FeltError:
            reply = "Felt can't be reached right now."
        chat_log.async_add_assistant_content_without_tools(
            conversation.AssistantContent(agent_id=user_input.agent_id, content=reply)
        )
        return conversation.async_get_result_from_chat_log(user_input, chat_log)
