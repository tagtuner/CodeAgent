from __future__ import annotations
import asyncio
from textual.app import App, ComposeResult
from textual.containers import Vertical, Horizontal, VerticalScroll
from textual.widgets import Header, Footer, Input, Static, RichLog, Label
from textual.binding import Binding
from textual.reactive import reactive
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from core.config import Config
from core.llm import LLMClient
from core.agent import Agent, AgentEvent
from core.session import Session
from core.router import Router
from tools.base import ToolRegistry


class StatusBar(Static):
    model_name = reactive("—")
    category = reactive("—")
    session_id = reactive("—")

    def render(self):
        return Text.from_markup(
            f" [bold cyan]Model:[/] {self.model_name}"
            f"  [bold green]Mode:[/] {self.category}"
            f"  [bold yellow]Session:[/] {self.session_id}"
        )


class ChatMessage(Static):
    def __init__(self, role: str, content: str, **kwargs):
        super().__init__(**kwargs)
        self.role = role
        self.msg_content = content

    def compose(self) -> ComposeResult:
        if self.role == "user":
            yield Static(
                Panel(self.msg_content, title="You", border_style="blue", expand=True),
            )
        elif self.role == "assistant":
            try:
                md = Markdown(self.msg_content)
            except Exception:
                md = self.msg_content
            yield Static(
                Panel(md, title="CodeAgent", border_style="green", expand=True),
            )
        elif self.role == "tool":
            yield Static(
                Panel(self.msg_content, title="Tool", border_style="yellow", expand=True),
            )
        elif self.role == "error":
            yield Static(
                Panel(self.msg_content, title="Error", border_style="red", expand=True),
            )


class CodeAgentTUI(App):
    CSS = """
    Screen {
        layout: vertical;
    }
    #chat-scroll {
        height: 1fr;
        border: solid $accent;
        padding: 0 1;
    }
    #input-area {
        dock: bottom;
        height: 3;
        padding: 0 1;
    }
    #status-bar {
        dock: bottom;
        height: 1;
        background: $surface;
        padding: 0 1;
    }
    ChatMessage {
        margin: 0 0 1 0;
    }
    """

    BINDINGS = [
        Binding("ctrl+n", "new_session", "New Session"),
        Binding("ctrl+l", "clear_chat", "Clear"),
        Binding("ctrl+q", "quit", "Quit"),
        Binding("escape", "quit", "Quit"),
    ]

    TITLE = "CodeAgent"

    def __init__(
        self,
        config: Config,
        registry: ToolRegistry,
        skills_context: str = "",
    ):
        super().__init__()
        self.config = config
        self.registry = registry
        self.skills_context = skills_context
        self.agent: Agent | None = None
        self.llm_main: LLMClient | None = None
        self.llm_fast: LLMClient | None = None
        self._processing = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(id="chat-scroll")
        yield StatusBar(id="status-bar")
        yield Input(placeholder="Type a message... (/new, /clear, /sessions, /quit)", id="input-area")
        yield Footer()

    async def on_mount(self):
        self.llm_main = LLMClient(self.config.main_model)
        if "fast" in self.config.models:
            self.llm_fast = LLMClient(self.config.fast_model)
        self._new_session()
        self.query_one("#input-area", Input).focus()

    def _new_session(self):
        max_tokens = self.config.session.get("max_history_tokens", 12000)
        session = Session(max_history_tokens=max_tokens)
        self.agent = Agent(
            config=self.config,
            llm_main=self.llm_main,
            llm_fast=self.llm_fast,
            registry=self.registry,
            session=session,
            skills_context=self.skills_context,
        )
        status = self.query_one("#status-bar", StatusBar)
        status.model_name = self.config.main_model.name
        status.session_id = session.id

    async def on_input_submitted(self, event: Input.Submitted):
        msg = event.value.strip()
        if not msg:
            return
        event.input.value = ""

        if msg.startswith("/"):
            await self._handle_command(msg)
            return

        if self._processing:
            return
        self._processing = True

        scroll = self.query_one("#chat-scroll", VerticalScroll)
        scroll.mount(ChatMessage("user", msg))
        scroll.scroll_end(animate=False)

        response_text = ""
        try:
            async for evt in self.agent.run(msg):
                if evt.type == "status":
                    status = self.query_one("#status-bar", StatusBar)
                    if evt.content.startswith("category:"):
                        status.category = evt.content.split(":")[1]
                elif evt.type == "text":
                    response_text = evt.content
                elif evt.type == "tool_start":
                    tool_msg = f"Calling {evt.tool_name}({evt.tool_args})"
                    scroll.mount(ChatMessage("tool", tool_msg))
                    scroll.scroll_end(animate=False)
                elif evt.type == "tool_result":
                    result_preview = evt.content[:500]
                    scroll.mount(ChatMessage("tool", f"Result from {evt.tool_name}:\n{result_preview}"))
                    scroll.scroll_end(animate=False)
                elif evt.type == "error":
                    scroll.mount(ChatMessage("error", evt.content))
                    scroll.scroll_end(animate=False)

            if response_text:
                scroll.mount(ChatMessage("assistant", response_text))
                scroll.scroll_end(animate=False)

            session_dir = self.config.session.get("dir", "/opt/codeagent/sessions")
            self.agent.session.save(session_dir)
        except Exception as e:
            scroll.mount(ChatMessage("error", f"Agent error: {e}"))
            scroll.scroll_end(animate=False)
        finally:
            self._processing = False

    async def _handle_command(self, cmd: str):
        scroll = self.query_one("#chat-scroll", VerticalScroll)
        parts = cmd.split(maxsplit=1)
        command = parts[0].lower()

        if command == "/new":
            self._new_session()
            for child in list(scroll.children):
                child.remove()
            scroll.mount(ChatMessage("tool", "New session started."))
        elif command == "/clear":
            for child in list(scroll.children):
                child.remove()
        elif command == "/sessions":
            session_dir = self.config.session.get("dir", "/opt/codeagent/sessions")
            sessions = Session.list_sessions(session_dir)
            if sessions:
                lines = [f"  {s['id']}  ({s['messages']} msgs)  {s['preview']}" for s in sessions[:10]]
                scroll.mount(ChatMessage("tool", "Recent sessions:\n" + "\n".join(lines)))
            else:
                scroll.mount(ChatMessage("tool", "No saved sessions."))
        elif command == "/model":
            scroll.mount(ChatMessage("tool", f"Main: {self.config.main_model.name}\nFast: {self.config.fast_model.name}"))
        elif command == "/tools":
            tools = self.registry.list_tools()
            scroll.mount(ChatMessage("tool", f"Available tools: {', '.join(tools)}"))
        elif command == "/quit":
            self.exit()
        else:
            scroll.mount(ChatMessage("tool", f"Unknown command: {command}\nAvailable: /new /clear /sessions /model /tools /quit"))
        scroll.scroll_end(animate=False)

    def action_new_session(self):
        asyncio.create_task(self._handle_command("/new"))

    def action_clear_chat(self):
        asyncio.create_task(self._handle_command("/clear"))
