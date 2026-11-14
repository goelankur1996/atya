"""A live speech-to-speech session with Amazon Nova 2 Sonic on Bedrock.

Audio in: 16 kHz mono 16-bit PCM. Audio out: 24 kHz mono 16-bit PCM.
Uses the bidirectional-stream SDK (aws-sdk-bedrock-runtime, HTTP/2 via AWS CRT);
credentials come from the server's IAM role like the rest of Bedrock.

    s = Sonic(system_prompt=..., on_audio=play, on_interrupt=stop_playing)
    await s.open()
    await s.say_first("The call just connected. Greet him.")   # optional
    await s.send_audio(pcm16k)                                 # as it arrives
    ...
    await s.close()
    s.transcript   # [("USER", "..."), ("ASSISTANT", "..."), ...]
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

MODEL = "amazon.nova-2-sonic-v1:0"
IN_RATE = 16_000
OUT_RATE = 24_000


def _event(name: str, body: dict) -> str:
    return json.dumps({"event": {name: body}})


class Sonic:
    def __init__(self, *, system_prompt: str, voice: str = "matthew", region: str = "us-east-1",
                 on_audio: Callable[[bytes], None] | None = None,
                 on_interrupt: Callable[[], None] | None = None,
                 on_text: Callable[[str, str], Awaitable[None] | None] | None = None):
        self.system_prompt = system_prompt
        self.voice = voice
        self.region = region
        self.on_audio = on_audio
        self.on_interrupt = on_interrupt
        self.on_text = on_text
        self.transcript: list[tuple[str, str]] = []
        self.prompt = str(uuid.uuid4())
        self.audio_content = str(uuid.uuid4())
        self.done = asyncio.Event()
        self.error: str | None = None
        self._client = None
        self._stream = None
        self._reader: asyncio.Task | None = None
        self._role: str | None = None
        self._final = True
        self._closing = False

    async def open(self) -> None:
        from aws_sdk_bedrock_runtime.client import AsyncBedrockRuntimeClient
        from aws_sdk_bedrock_runtime.config import AsyncBedrockRuntimeConfig
        from aws_sdk_bedrock_runtime.models import InvokeModelWithBidirectionalStreamOperationInput
        from smithy_http.aio.crt import AWSCRTHTTPClient

        config = await AsyncBedrockRuntimeConfig.resolve(region=self.region, transport=AWSCRTHTTPClient())
        self._client = AsyncBedrockRuntimeClient(config=config)
        self._stream = await self._client.invoke_model_with_bidirectional_stream(
            InvokeModelWithBidirectionalStreamOperationInput(model_id=MODEL))
        system = str(uuid.uuid4())
        for e in (
            _event("sessionStart", {"inferenceConfiguration": {"maxTokens": 1024, "topP": 0.9, "temperature": 0.7}}),
            _event("promptStart", {
                "promptName": self.prompt,
                "textOutputConfiguration": {"mediaType": "text/plain"},
                "audioOutputConfiguration": {"mediaType": "audio/lpcm", "sampleRateHertz": OUT_RATE,
                                             "sampleSizeBits": 16, "channelCount": 1, "voiceId": self.voice,
                                             "encoding": "base64", "audioType": "SPEECH"}}),
            _event("contentStart", {"promptName": self.prompt, "contentName": system, "type": "TEXT",
                                    "interactive": False, "role": "SYSTEM",
                                    "textInputConfiguration": {"mediaType": "text/plain"}}),
            _event("textInput", {"promptName": self.prompt, "contentName": system, "content": self.system_prompt}),
            _event("contentEnd", {"promptName": self.prompt, "contentName": system}),
            _event("contentStart", {"promptName": self.prompt, "contentName": self.audio_content, "type": "AUDIO",
                                    "interactive": True, "role": "USER",
                                    "audioInputConfiguration": {"mediaType": "audio/lpcm", "sampleRateHertz": IN_RATE,
                                                                "sampleSizeBits": 16, "channelCount": 1,
                                                                "audioType": "SPEECH", "encoding": "base64"}}),
        ):
            await self._send(e)
        self._reader = asyncio.create_task(self._read())

    async def say_first(self, instruction: str) -> None:
        """A text turn from the user's side, so the model speaks first (e.g. a greeting)."""
        name = str(uuid.uuid4())
        await self._send(_event("contentStart", {"promptName": self.prompt, "contentName": name, "type": "TEXT",
                                                 "interactive": True, "role": "USER",
                                                 "textInputConfiguration": {"mediaType": "text/plain"}}))
        await self._send(_event("textInput", {"promptName": self.prompt, "contentName": name, "content": instruction}))
        await self._send(_event("contentEnd", {"promptName": self.prompt, "contentName": name}))

    async def send_audio(self, pcm16k: bytes) -> None:
        if self._closing or self.done.is_set() or not pcm16k:
            return
        await self._send(_event("audioInput", {"promptName": self.prompt, "contentName": self.audio_content,
                                               "content": base64.b64encode(pcm16k).decode()}))

    async def close(self) -> None:
        if self._stream is None or self._closing:
            return
        self._closing = True
        try:
            await self._send(_event("contentEnd", {"promptName": self.prompt, "contentName": self.audio_content}))
            await self._send(_event("promptEnd", {"promptName": self.prompt}))
            await self._send(_event("sessionEnd", {}))
            await self._stream.input_stream.close()
        except Exception:
            log.debug("closing the Sonic stream failed", exc_info=True)
        if self._reader:
            try:
                await asyncio.wait_for(self._reader, timeout=5)
            except (asyncio.TimeoutError, Exception):
                self._reader.cancel()
        self.done.set()

    async def _send(self, event_json: str) -> None:
        from aws_sdk_bedrock_runtime.models import BidirectionalInputPayloadPart, InvokeModelWithBidirectionalStreamInputChunk

        await self._stream.input_stream.send(InvokeModelWithBidirectionalStreamInputChunk(
            value=BidirectionalInputPayloadPart(bytes_=event_json.encode())))

    async def _read(self) -> None:
        from aws_sdk_bedrock_runtime.models import InvokeModelWithBidirectionalStreamOutputChunk

        try:
            _, output = await self._stream.await_output()
            async for item in output:
                if not isinstance(item, InvokeModelWithBidirectionalStreamOutputChunk):
                    msg = getattr(getattr(item, "value", None), "message", None) or type(item).__name__
                    self.error = msg
                    log.warning("Sonic stream error: %s", msg)
                    break
                if not item.value.bytes_:
                    continue
                await self._handle(json.loads(item.value.bytes_.decode()).get("event", {}))
                if self.done.is_set():
                    break
        except Exception as e:
            if not self._closing:
                self.error = f"{type(e).__name__}: {e}"
                log.warning("Sonic stream ended: %s", self.error)
        finally:
            self.done.set()

    async def _handle(self, ev: dict) -> None:
        if "contentStart" in ev:
            c = ev["contentStart"]
            self._role = c.get("role")
            stage = None
            try:
                stage = json.loads(c.get("additionalModelFields") or "{}").get("generationStage")
            except ValueError:
                pass
            self._final = stage != "SPECULATIVE"
        elif "textOutput" in ev:
            text = ev["textOutput"].get("content", "")
            if '"interrupted"' in text:
                if self.on_interrupt:
                    self.on_interrupt()
                return
            role = ev["textOutput"].get("role") or self._role or "ASSISTANT"
            if role == "USER" or self._final:
                self.transcript.append((role, text))
                if self.on_text:
                    r = self.on_text(role, text)
                    if asyncio.iscoroutine(r):
                        await r
        elif "audioOutput" in ev:
            if self.on_audio:
                self.on_audio(base64.b64decode(ev["audioOutput"]["content"]))
        elif "completionEnd" in ev:
            log.info("Sonic: completion ended (%s)", ev["completionEnd"].get("stopReason"))
