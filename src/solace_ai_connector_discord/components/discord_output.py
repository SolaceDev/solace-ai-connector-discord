import base64
import re
import json
from datetime import datetime
import threading
import queue
import asyncio
from dataclasses import dataclass
import io

from prettytable import PrettyTable

from solace_ai_connector.common.message import Message
from solace_ai_connector.common.log import log
from .discord_base import DiscordBase, FeedbackEndpoint
from discord import File, ButtonStyle, Message as DiscordMessage, Interaction, InteractionType, ComponentType, Thread, Intents, User
from discord.context_managers import Typing
from discord.ui import Button, View, Modal, TextInput
from discord.ext.commands import Bot
import requests


info = {
    "class_name": "DiscordOutput",
    "description": (
        "Discord output component. The component sends messages to Discord channels using the Bolt API."
    ),
    "config_parameters": [
        {
            "name": "discord_bot_token",
            "type": "string",
            "description": "The Discord bot token to connect to Discord.",
        },
        {
            "name": "share_discord_connection",
            "type": "string",
            "description": "Share the Discord connection with other components in this instance.",
        },
        {
            "name": "correct_markdown_formatting",
            "type": "boolean",
            "description": "Correct markdown formatting in messages to conform to Discord markdown.",
            "default": "true",
        },
        {
            "name": "feedback",
            "type": "boolean",
            "description": "Collect thumbs up/thumbs down from users.",
        },
        {
            "name": "feedback_post_url",
            "type": "string",
            "description": "URL to send feedback to.",
        },
        {
            "name": "feedback_post_headers",
            "type": "object",
            "description": "Headers to send with feedback post.",
        }
    ],
    "input_schema": {
        "type": "object",
        "properties": {
            "message_info": {
                "type": "object",
                "properties": {
                    "channel": {
                        "type": "string",
                    },
                    "client_msg_id": {
                        "type": "string",
                    },
                    "ts": {
                        "type": "string",
                    },
                    "event_ts": {
                        "type": "string",
                    },
                    "channel_type": {
                        "type": "string",
                    },
                    "user_id": {
                        "type": "string",
                    },
                    "session_id": {
                        "type": "string",
                    },
                },
                "required": ["channel", "session_id"],
            },
            "content": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                    },
                    "files": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {
                                    "type": "string",
                                },
                                "content": {
                                    "type": "string",
                                },
                                "mime_type": {
                                    "type": "string",
                                },
                                "filetype": {
                                    "type": "string",
                                },
                                "size": {
                                    "type": "number",
                                },
                            },
                        },
                    },
                },
            },
        },
        "required": ["message_info", "content"],
    },
}


class DiscordOutput(DiscordBase):
    def __init__(self, **kwargs):
        super().__init__(info, **kwargs)
        self.fix_formatting = self.get_config("correct_markdown_formatting", True)
        self.streaming_state = {}
        self.discord_message_response_queue = queue.Queue()
        bot_intents = Intents.default()
        bot_intents.message_content = True

        self.app = Bot(command_prefix=self.command_prefix, intents=bot_intents)

        DiscordSender(
            app=self.app,
            discord_bot_token=self.discord_bot_token,
            input_queue=self.discord_message_response_queue,
            feedback_endpoint=self.feedback_endpoint
        ).start()

    def invoke(self, message, data):
        content = data.get("content")
        message_info = data.get("message_info")

        text = content.get("text")
        uuid = content.get("uuid")
        user_id = content.get("user_id")
        files = content.get("files")
        streaming = content.get("streaming")
        status_update = content.get("status_update")
        response_complete = content.get("response_complete")
        last_chunk = content.get("last_chunk")
        first_chunk = content.get("first_chunk")

        thread_ts = message_info.get("ts")
        channel = message_info.get("channel")
        ack_msg_ts = message_info.get("ack_msg_ts")

        feedback_data = data.get("feedback_data", {})

        if response_complete:
            status_update = True
            text = ":checkered_flag: Response complete"
        elif status_update:
            text = ":thinking_face: " + text

        if not channel:
            log.error("discord_output: No channel specified in message")
            self.discard_current_message()
            return None

        return {
            "text": text,
            "uuid": uuid,
            "files": files,
            "streaming": streaming,
            "channel": channel,
            "thread_ts": thread_ts,
            "ack_msg_ts": ack_msg_ts,
            "status_update": status_update,
            "last_chunk": last_chunk,
            "first_chunk": first_chunk,
            "response_complete": response_complete,
            "feedback_data": feedback_data,
            "user_id": user_id,
        }

    def send_message(self, message: Message):
        self.discord_message_response_queue.put(message)
        super().send_message(message)

    def fix_markdown(self, message):
        # Fix links - the LLM is very stubborn about giving markdown links
        # Find [text](http...) and replace with <http...|text>
        message = re.sub(r"\[(.*?)\]\((http.*?)\)", r"<\2|\1>", message)
        # Remove the language specifier from code blocks
        message = re.sub(r"```[a-z]+\n", "```", message)
        # Fix bold
        message = re.sub(r"\*\*(.*?)\*\*", r"*\1*", message)

        # Reformat a table to be Discord compatible
        message = self.convert_markdown_tables(message)

        return message

    def get_streaming_state(self, uuid):
        return self.streaming_state.get(uuid)

    def add_streaming_state(self, uuid):
        state = {
            "create_time": datetime.now(),
        }
        self.streaming_state[uuid] = state
        self.age_out_streaming_state()
        return state

    def delete_streaming_state(self, uuid):
        try:
            del self.streaming_state[uuid]
        except KeyError:
            pass

    def age_out_streaming_state(self, age=60):
        # Note that we can later optimize this by using an array of streaming_state that
        # is ordered by create_time and then we can just remove the first element until
        # we find one that is not expired.
        now = datetime.now()
        for uuid, state in list(self.streaming_state.items()):
            if (now - state["create_time"]).total_seconds() > age:
                del self.streaming_state[uuid]

    def convert_markdown_tables(self, message):
        def markdown_to_fixed_width(match):
            table_str = match.group(0)
            rows = [
                line.strip().split("|")
                for line in table_str.split("\n")
                if line.strip()
            ]
            headers = [cell.strip() for cell in rows[0] if cell.strip()]

            pt = PrettyTable()
            pt.field_names = headers

            for row in rows[2:]:
                pt.add_row([cell.strip() for cell in row if cell.strip()])

            return f"\n```\n{pt.get_string()}\n```\n"

        pattern = r"\|.*\|[\n\r]+\|[-:| ]+\|[\n\r]+((?:\|.*\|[\n\r]+)+)"
        return re.sub(pattern, markdown_to_fixed_width, message)

@dataclass(slots=True)
class Feedback(Modal, title=''):
    endpoint: FeedbackEndpoint

    feedback = TextInput(
        label='Feedback',
        placeholder='How can we improve this response?',
        required=False,
        max_length=300,
    )

    async def on_submit(self, interaction: Interaction):
        await interaction.response.send_message(f'Thanks for your feedback, {interaction.user.mention}!', ephemeral=True)

        rest_body = {
            "user": interaction.user.id,
            "feedback": self.feedback.value,
            "interface": "discord",
            "interface_data": {
                "channel": interaction.channel_id
            },
            "data": "{}"
        }

        if self.feedback.value:
            rest_body["feedback_reason"] = self.feedback.value

        try:
            requests.post(
                url=self.endpoint.url,
                headers=self.endpoint.headers,
                data=json.dumps(rest_body)
            )
        except Exception as e:
            log.error(f"Failed to post feedback: {str(e)}")

    async def on_error(self, interaction: Interaction, error: Exception) -> None:
        await interaction.response.send_message('Oops! Something went wrong.', ephemeral=True)

        log.error("Erro while trying to post feedback", exc_info=error)

@dataclass(slots=True)
class ActiveState:
    message: DiscordMessage

@dataclass(slots=True)
class TypingState:
    typing: Typing
    handles: int = 1
    stop_event: asyncio.Event = asyncio.Event()

@dataclass(slots=True)
class State:
    text: str = ""
    last_edit: float = 0
    lock: asyncio.Lock = asyncio.Lock()
    active: ActiveState | None = None
    complete: bool = False

class DiscordSender(threading.Thread):
    def __init__(
        self,
        app: Bot,
        discord_bot_token,
        input_queue: queue.Queue[Message],
        feedback_endpoint: FeedbackEndpoint | None
    ):
        threading.Thread.__init__(self)
        self.app = app
        self.discord_bot_token = discord_bot_token
        self.input_queue = input_queue
        self.feedback_endpoint = feedback_endpoint
        self.state_by_uuid: dict[str, State] = {}
        self.typing_tasks: dict[int, TypingState] = {}

    def run(self):
        asyncio.run(self.really_run())

    @staticmethod
    def create_feedback_view() -> View:
        thumbsup_button = Button(label="👍", style=ButtonStyle.green, custom_id="thumbs_up")
        thumbsdown_button = Button(label="👎", style=ButtonStyle.red, custom_id="thumbs_down")

        view = View()
        view.add_item(thumbsup_button)
        view.add_item(thumbsdown_button)

        return view

    async def send_message(self, message: Message):
        uuid = message.get_data("previous:uuid")
        if not isinstance(uuid, str): return

        if uuid not in self.state_by_uuid:
            self.state_by_uuid[uuid] = state = State()
        else:
            state = self.state_by_uuid[uuid]

        async with state.lock:
            try:
                await self.__send_message(message, state)
            except Exception as e:
                log.error("Error sending discord message", exc_info=e)

    def __should_prepare_message(self, message: Message, state: State, text: str) -> bool:
        last_chunk = message.get_data("previous:last_chunk")
        response_complete = message.get_data("previous:response_complete")

        if response_complete is None:
            response_complete = False

        assert isinstance(response_complete, bool)

        state.complete = response_complete

        if len(text) <= len(state.text) and not last_chunk:
            return False

        now = datetime.now().timestamp()

        # throttle responses
        if last_chunk or response_complete or now - state.last_edit > 1_200:
            state.last_edit = now
        else:
            return False

        return True

    async def __prepare_message(self, message: Message, state: State) -> tuple[Thread | User, View | None, str] | None:
        channel = message.get_data("previous:channel")
        messages = message.get_data("previous:text")
        properties = message.get_user_properties()
        user_id = properties.get("user_id")

        assert isinstance(channel, int)
        assert isinstance(user_id, int)

        if not isinstance(messages, list):
            if messages is not None:
                messages = [messages]
            else:
                messages = []

        text = ""

        for part in messages:
            if not part or not isinstance(part, str):
                continue
            text += part

        if not self.__should_prepare_message(message, state, text):
            return

        text_channel = await self.app.fetch_user(user_id) if user_id == channel else self.app.get_channel(channel)
        if not isinstance(text_channel, (Thread, User)):
            return

        if len(text) > 2000:
            text = text[:2000]

        view = self.create_feedback_view() if state.complete and self.feedback_endpoint is not None else None
        text = text or "\u200b"

        return text_channel, view, text

    async def __send_message(self, message: Message, state: State):
        files = message.get_data("previous:files") or []
        last_chunk = message.get_data("previous:last_chunk")

        assert isinstance(files, list)

        content = await self.__prepare_message(message, state)
        if not content: return

        channel, view, text = content

        if not state.active:
            sent_message = await channel.send(text, view=view) if view else await channel.send(text)
            state.active = ActiveState(message=sent_message)
            await self.start_typing(channel)
        else:
            await state.active.message.edit(content=text, view=view)

        files_to_add = []

        for file in files:
            file_content = base64.b64decode(file["content"])
            files_to_add.append(File(fp=io.BytesIO(file_content), filename=file["name"]))

        if files_to_add:
            await state.active.message.add_files(*files_to_add)

        if last_chunk or state.complete:
            await self.stop_typing(channel)

    async def really_run(self):
        await self.app.login(self.discord_bot_token)

        asyncio.create_task(self.do_messages())
        self.register_action_handlers()

        await self.app.connect()
    
    async def do_messages(self):
        while True:
            try:
                message = self.input_queue.get_nowait()
                asyncio.create_task(self.send_message(message))
            except queue.Empty:
                await asyncio.sleep(0.01)

    def register_action_handlers(self):
        """
            _summary_ : Register the action handlers for the Discord bot
        """

        @self.app.event
        async def on_ready():
            await self.app.tree.sync()

        @self.app.event
        async def on_interaction(interaction: Interaction):
            if interaction.type != InteractionType.component:
                return
            if not interaction.data:
                return
            if "component_type" not in interaction.data or interaction.data["component_type"] != ComponentType.button.value:
                return

            custom_id = interaction.data["custom_id"]

            if interaction.message:
                await interaction.message.edit(view=None)

            match custom_id:
                case "thumbs_up": await self.thumbs_up_callback(interaction)
                case "thumbs_down": await self.thumbs_down_callback(interaction)

        @self.app.tree.command(name = "help")
        async def help(interaction: Interaction):
            await interaction.response.send_message("""
# Hello! I'm Solly Chat

I'm an AI assistant with agentic capabilities designed to help you with a variety of tasks. Here's what I can do:

## General Capabilities
- Answer questions using my knowledge base
- Have conversations on a wide range of topics
- Provide reasoning and analysis
- Generate creative content
- Assist with problem-solving

## Specialized Features

### Document & File Handling
- Convert various file formats (PDF, Word, Excel, HTML, CSV, PPTX, ZIP) to Markdown
- Create and retrieve text files
- Process file content

### Data Visualization
- Generate custom charts and graphs using Plotly
- Create PlantUML diagrams (sequence, class, use case, activity, component, state, and timing diagrams)

### Web Capabilities
- Search the web for current information
- Fetch and process content from websites
- Search for news and current events
- Find images online
- Download external files from the web

### Image Processing
- Generate images based on text descriptions
- Analyze and describe the content of images

## How to Use Me
Simply DM or @ me with questions or requests in natural language, and I'll use my capabilities to assist you. For complex tasks, I might break them down into steps and keep you updated on my progress.""", ephemeral=True)

    async def thumbs_up_callback(self, interaction: Interaction):
        await interaction.response.send_message("You clicked thumbs up!", ephemeral=True)

    async def thumbs_down_callback(self, interaction: Interaction):
        assert self.feedback_endpoint is not None

        await interaction.response.send_modal(Feedback(
            endpoint=self.feedback_endpoint))

    async def __start_typing_state(self, state: TypingState):
        while not state.stop_event.is_set():
            await asyncio.sleep(5)
            await state.typing.wrapped_typer()

    async def start_typing(self, channel: Thread | User):
        """Start a typing indicator in the specified channel"""

        if channel.id not in self.typing_tasks:
            state = TypingState(typing=channel.typing())
            self.typing_tasks[channel.id] = state
            asyncio.create_task(self.__start_typing_state(state))
        else:
            self.typing_tasks[channel.id].handles += 1

    async def stop_typing(self, channel: Thread | User):
        """Stop the typing indicator in the specified channel"""

        if channel.id not in self.typing_tasks:
            return

        state = self.typing_tasks[channel.id]

        if state.handles == 1:
            self.typing_tasks.pop(channel.id)
            state.stop_event.set()
        else:
            state.handles -= 1
