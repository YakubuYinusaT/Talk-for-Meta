"""The streamed call. Asterisk answers the trunk's call and connects it to this server over AudioSocket; each connection is one call.

Everything a caller says or types goes through the same turn code as a call over the callback path (sofa/routes/voice.py: run_turn, run_digits), so the two
behave alike. What this adds is the line itself: hearing when the caller has finished speaking (no recording to wait for), playing the reply at once, and
stopping when the caller talks over it.
"""

import asyncio
import logging
import time
import uuid
from collections import deque

from ..audio import pcm8_to_wav16
from ..dialogue.templates import render, speak_lang
from ..routes import voice
from ..textutil import e164
from .audiosocket import AUDIO, DTMF, ERROR, FRAME_BYTES, FRAME_MS, HANGUP, ID, number_from_uuid, pack, read_frame, uuid_text
from .vad import Endpointer

log = logging.getLogger("sofa.sip")

FRAME_SECONDS = FRAME_MS / 1000
LEAD_SECONDS = 0.1  # audio is sent this far ahead of real time: enough to ride out a busy moment, short enough that stopping Talk is quick


class StreamedCall:
    def __init__(self, svc, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.svc, self.s = svc, svc.settings
        self.reader, self.writer = reader, writer
        s = self.s
        self.ep = Endpointer(s.sip_voice_rms, end_ms=s.sip_end_silence_ms, min_speech_ms=s.sip_min_speech_ms, max_ms=s.sip_max_speech_seconds * 1000)
        self.barge_frames = max(1, s.sip_barge_ms // FRAME_MS)
        self.utts: deque = deque()       # what the caller said, one entry per stretch of speech
        self.keys: deque = deque()       # keypad digits
        self.gone = asyncio.Event()      # the caller hung up
        self.wake = asyncio.Event()      # something arrived (speech, a digit, hang up)
        self.barge = asyncio.Event()     # the caller talked over Talk
        self.playing = False             # Talk is speaking and may be stopped
        self.armed = True                # the caller can stop Talk only by starting to speak AFTER Talk began: noise already on the line does not count
        self.barges = 0                  # how often the caller stopped Talk (for the log of a call)
        self.session_id = ""
        self.pending: asyncio.Future | None = None  # a turn still being worked out
        self.turns = 0
        self.last_frame = 0.0
        self.asked = time.perf_counter()

    # ---- the line -----------------------------------------------------------------------------------------------------------------

    async def pump(self) -> None:
        """Read the line all the time: speech is cut out as it comes, so the caller never waits for a recording to end."""
        try:
            while True:
                frame = await read_frame(self.reader)
                if frame is None or frame[0] in (HANGUP, ERROR):
                    break
                kind, payload = frame
                if kind == AUDIO:
                    self.last_frame = asyncio.get_running_loop().time()
                    utt = self.ep.feed(payload)
                    if not self.ep.speaking:
                        self.armed = True
                    if self.playing and self.armed and self.ep.run >= self.barge_frames and not self.barge.is_set():
                        self.barge.set()
                        self.barges += 1
                    if utt:
                        self.utts.append(utt)
                        self.wake.set()
                elif kind == DTMF:
                    self.keys.append(payload.decode("ascii", "ignore"))  # never logged: it may be a PIN
                    self.wake.set()
        finally:
            self.gone.set()
            self.wake.set()

    async def watch(self) -> None:
        """Speech does not end if the line stops sending: some trunks send no audio at all while the caller is quiet. So the quiet is also timed by the clock."""
        loop = asyncio.get_running_loop()
        quiet = self.s.sip_end_silence_ms / 1000 + 0.2
        while not self.gone.is_set():
            await asyncio.sleep(0.1)
            if self.ep.speaking and loop.time() - self.last_frame > quiet:
                utt = self.ep.finish()
                if utt:
                    self.utts.append(utt)
                    self.wake.set()

    async def wait_for(self, ready, timeout: float) -> bool:
        """Wait until `ready()` is true, the call is gone or the time is up. Nothing can arrive between the check and the clear: there is no await there."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not ready() and not self.gone.is_set():
            self.wake.clear()
            left = deadline - loop.time()
            if left <= 0:
                break
            try:
                await asyncio.wait_for(self.wake.wait(), left)
            except asyncio.TimeoutError:
                break
        return ready()

    async def play(self, pcm: bytes, barge: bool = False) -> bool:
        """Send audio at the pace of speech. False when it was cut short: the caller talked over it (if that is allowed) or hung up."""
        self.barge.clear()
        self.armed = not self.ep.speaking  # speech already going on when Talk starts (a noisy line) is not the caller talking over it
        self.playing = barge and self.s.sip_barge_in
        loop = asyncio.get_running_loop()
        start, sent = loop.time(), 0
        try:
            for off in range(0, len(pcm), FRAME_BYTES):
                if self.gone.is_set() or (self.playing and self.barge.is_set()):
                    return False
                self.writer.write(pack(AUDIO, pcm[off:off + FRAME_BYTES].ljust(FRAME_BYTES, b"\0")))
                await self.writer.drain()
                sent += 1
                wait = start + sent * FRAME_SECONDS - loop.time() - LEAD_SECONDS
                if wait > 0:
                    await asyncio.sleep(wait)
            await asyncio.sleep(max(0.0, start + sent * FRAME_SECONDS - loop.time()))  # the last of it is still on its way to the caller
            return True
        except (ConnectionError, OSError):
            self.gone.set()
            return False
        finally:
            self.playing = False

    async def hang_up(self) -> None:
        try:
            self.writer.write(pack(HANGUP))
            await self.writer.drain()
        except (ConnectionError, OSError):
            pass
        self.writer.close()

    # ---- speaking ------------------------------------------------------------------------------------------------------------------

    def language(self) -> str:
        st = self.svc.sessions.get(self.session_id)
        return st["lang"] if st else "en"

    async def say(self, text: str, lang: str, barge: bool = False) -> bool:
        pcm = await self.svc.audio.speak_telephone(text, lang)
        return await self.play(pcm, barge) if pcm else True

    async def say_key(self, key: str) -> bool:
        vlang = speak_lang(key, self.language())
        return await self.say(render(key, vlang, {}), vlang)

    def voice(self, clips: list) -> list[asyncio.Future]:
        """Start voicing a reply, a sentence at a time. The sentences are made side by side (the voice service limits how many at once) and spoken in order,
        so Talk starts speaking when the first is made, not when the last is."""
        audio = self.svc.audio
        return [asyncio.ensure_future(audio.speak_telephone(piece, lang)) for text, lang in clips for piece in audio.pieces(text, lang)]

    async def say_stream(self, clips: list, outcome, barge: bool) -> bool:
        """A reply: its sentences one after another, each as soon as it is voiced. Anything heard while Talk was thinking is dropped (a "hello?" must not
        become the next turn), and so is anything that ended while Talk spoke, unless it stopped Talk. A reply worded by the model that is slow to voice is
        replaced by its stored template wording, as on the callback path: never a long silence."""
        self.utts.clear()
        template = outcome.extra.get("template") if outcome.action.endswith("|generated") else None
        tasks = self.voice(clips)
        try:
            if template and tasks:
                try:
                    await asyncio.wait_for(asyncio.shield(tasks[0]), self.s.tts_budget_seconds)
                except Exception as exc:  # too slow, or the voice failed
                    log.warning("voice too slow for the worded reply (%s), speaking the template", type(exc).__name__)
                    for t in tasks:
                        t.cancel()
                    text, lang, parts = template
                    outcome.text, outcome.lang, outcome.parts = text, lang, parts
                    outcome.action = outcome.action.replace("|generated", "|template_fallback")
                    tasks = self.voice([*(parts or [(text, lang)]), *outcome.also])
            for i, task in enumerate(tasks):
                pcm = await task
                if i == 0:
                    log.info("streamed turn %d: first sound %d ms after the caller finished", self.turns, int((time.perf_counter() - self.asked) * 1000))
                if pcm and not await self.play(pcm, barge):
                    return False
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
        self.utts.clear()
        return True

    # ---- hearing -------------------------------------------------------------------------------------------------------------------

    async def hear_speech(self):
        self.keys.clear()
        if not await self.wait_for(lambda: bool(self.utts), self.s.sip_silence_seconds):
            if self.gone.is_set():
                return "gone", None
            if not self.ep.speaking or not await self.wait_for(lambda: bool(self.utts), self.s.sip_max_speech_seconds + 2):  # still mid-sentence: let it finish
                return ("gone", None) if self.gone.is_set() else ("silence", None)
        return "speech", self.utts.popleft()

    async def hear_digits(self, count: int):
        got = ""
        while True:
            ready = await self.wait_for(lambda: bool(self.keys) or bool(self.utts), self.s.digits_timeout_seconds)
            if self.gone.is_set():
                return "gone", None
            if self.keys:
                while self.keys:
                    key = self.keys.popleft()
                    if key == "#":
                        return "digits", got
                    if key.isdigit() and len(got) < count:
                        got += key
                if len(got) >= count:
                    return "digits", got
                continue
            if self.utts:  # the caller spoke instead of typing
                return "speech", self.utts.popleft()
            if not ready:
                return "digits", got  # the time ran out: what was typed so far, as the callback path does

    # ---- one turn ------------------------------------------------------------------------------------------------------------------

    async def work(self, job):
        """Run a turn, with a short holding message when it takes long, and more of them while it goes on, so the caller never sits in silence.
        Returns (outcome, clips), or None when the reply is not coming (the caller is told, and listened to)."""
        task = self.pending = asyncio.ensure_future(job)
        done, _ = await asyncio.wait({task}, timeout=self.s.sip_filler_after_seconds)
        stage = 0
        while not done and not self.gone.is_set():
            if stage > voice.WAIT_STAGES:
                for key in ("wait_giveup", "gateway_anything_else"):
                    await self.say_key(key)
                return None
            await self.say_key("filler" if stage == 0 else f"wait_{stage}")
            stage += 1
            done, _ = await asyncio.wait({task}, timeout=self.s.filler_every_seconds)
        if not done:
            return None
        try:
            return task.result()
        except Exception:
            log.exception("streamed turn failed")
            await self.say_key("repeat_prompt")
            return None

    async def conversation(self, caller: str) -> None:
        svc, s = self.svc, self.s
        opened = await voice.open_gateway_call(svc, self.session_id, caller, e164(s.gateway_number), s.at_callback_secret)
        if opened is None:  # this caller has phoned too often
            return
        await self.say(*opened)
        self.utts.clear()
        keypad = 0  # how many digits the last reply asked for, if it did
        while not self.gone.is_set():
            kind, value = await (self.hear_digits(keypad) if keypad else self.hear_speech())
            if kind == "gone":
                break
            self.asked = time.perf_counter()  # the speech has ended (the pause that ends it is already over)
            if kind == "digits":
                job = voice.run_digits(svc, self.session_id, value, uuid.uuid4(), stream=True)
            elif kind == "speech":
                wav = await asyncio.to_thread(pcm8_to_wav16, value.pcm)
                job = voice.run_turn(svc, self.session_id, "stream", uuid.uuid4(), wav16=wav, stream=True)
            else:  # nobody spoke
                job = voice.run_turn(svc, self.session_id, None, uuid.uuid4(), stream=True)
            result = await self.work(job)
            if result is None:
                keypad = 0
                continue
            outcome, clips = result
            self.turns += 1
            log.info("streamed turn %d: reply worded %d ms after the caller finished (%s)", self.turns, int((time.perf_counter() - self.asked) * 1000), outcome.action)
            await self.say_stream(clips, outcome, barge=not outcome.collect and not outcome.end_call)
            if outcome.end_call:
                break
            keypad = int(outcome.collect["digits"]) if outcome.collect else 0

    async def run(self) -> None:
        first = await read_frame(self.reader)
        if not first or first[0] != ID or len(first[1]) != 16:
            log.warning("streamed connection dropped: it did not begin with a call id (%s)", "closed at once" if not first else f"kind {first[0]}, {len(first[1])} bytes")
            return
        self.session_id = uuid_text(first[1])
        caller = number_from_uuid(self.session_id)
        if not caller:  # a hidden number would be one shared customer record: no call is taken without a number
            log.info("streamed call %s turned away: no caller number", self.session_id)
            await self.hang_up()
            return
        began = time.monotonic()
        log.info("streamed call %s started", self.session_id)
        pump, watch = asyncio.ensure_future(self.pump()), asyncio.ensure_future(self.watch())
        try:
            await asyncio.wait_for(self.conversation(caller), timeout=self.s.max_call_seconds + 90)
        except asyncio.TimeoutError:
            log.warning("streamed call %s ran past its limit", self.session_id)
        finally:
            pump.cancel()
            watch.cancel()
            log.info("streamed call %s line: %d frames heard, loudest %d, noise level %d, speech needs over %d, Talk stopped by the caller %d times", self.session_id,
                     self.ep.frames, self.ep.peak, self.ep.floor, self.ep.threshold, self.barges)
            if self.pending and not self.pending.done():  # a turn still running is allowed to finish before the call is closed
                await asyncio.wait({self.pending}, timeout=30)
            voice.close_call(self.svc, self.session_id, int(time.monotonic() - began))
            await self.hang_up()
            log.info("streamed call %s ended after %d s, %d turns", self.session_id, int(time.monotonic() - began), self.turns)


class SipServer:
    """Listens on the local AudioSocket port. Only Asterisk, on the same machine, connects to it."""

    def __init__(self, svc):
        self.svc, self.active, self.server = svc, 0, None

    async def start(self) -> None:
        s = self.svc.settings
        self.server = await asyncio.start_server(self.handle, s.sip_host, s.sip_port)
        log.info("streamed calls: listening on %s:%s", s.sip_host, s.sip_port)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.active >= self.svc.settings.sip_max_calls:
            log.warning("streamed connection turned away: %d calls already", self.active)
            writer.close()
            return
        self.active += 1
        try:
            await StreamedCall(self.svc, reader, writer).run()
        except Exception:
            log.exception("streamed call failed")
        finally:
            self.active -= 1
            writer.close()

    def stop(self) -> None:
        if self.server:
            self.server.close()
