"""Streamed calls: the AudioSocket protocol, hearing where speech starts and ends, talking over Talk, keypad digits, and a whole call through the agent."""

import asyncio
import io
import struct
import uuid
import wave
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from scripts.seed import seed
from sofa.audio import _wav_to_slin_plain, pcm8_to_wav16
from sofa.clients.asr import ASRResult
from sofa.config import Settings
from sofa.main import create_app
from sofa.models import Call, CallTurn
from sofa.sip import agent as agent_mod
from sofa.sip.agent import SipServer, StreamedCall
from sofa.sip.audiosocket import AUDIO, DTMF, FRAME_BYTES, HANGUP, ID, number_from_uuid, pack, read_frame, uuid_for_number
from sofa.sip.vad import Endpointer, frame_rms

CALLER = "+2348055550123"
QUIET = b"\x00\x00" * 160


def loud(level: int = 3000) -> bytes:
    """A 20 ms frame of a square wave: speech as far as the endpointer can tell."""
    return struct.pack("<160h", *([level, -level] * 80))


# ---- the protocol -------------------------------------------------------------------------------------------------------------------


def test_a_frame_is_kind_length_and_payload():
    assert pack(AUDIO, b"ab") == b"\x10\x00\x02ab"
    assert pack(HANGUP) == b"\x00\x00\x00"


def test_frames_are_read_back_and_the_end_of_the_line_is_none():
    async def go():
        reader = asyncio.StreamReader()
        reader.feed_data(pack(DTMF, b"7") + pack(AUDIO, QUIET) + pack(HANGUP))
        reader.feed_eof()
        return [await read_frame(reader) for _ in range(4)]
    assert asyncio.run(go()) == [(DTMF, b"7"), (AUDIO, QUIET), (HANGUP, b""), None]


@pytest.mark.parametrize("number", ["+2348055550123", "+14155550100", "+447911123456"])
def test_the_dialplan_hides_the_callers_number_in_the_call_id_and_it_comes_back(number):
    assert number_from_uuid(uuid_for_number(number)) == number


def test_a_number_without_its_leading_zero_or_country_code_is_a_nigerian_one():
    assert number_from_uuid(uuid_for_number("08055550123")) == "+2348055550123"


def test_a_hidden_number_gives_no_caller():
    assert number_from_uuid(str(uuid.UUID(int=0))) == ""


# ---- audio ---------------------------------------------------------------------------------------------------------------------------


def test_telephone_audio_becomes_a_16_khz_wav_with_twice_the_samples():
    pcm = struct.pack("<4h", 0, 100, 200, 300)
    with wave.open(io.BytesIO(pcm8_to_wav16(pcm))) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()) == (16000, 1, 2, 8)
        assert struct.unpack("<8h", w.readframes(8)) == (0, 50, 100, 150, 200, 250, 300, 300)


def test_a_voice_clip_is_brought_down_to_8_khz_without_ffmpeg():
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(24000)
        w.writeframes(struct.pack("<2400h", *([1000] * 2400)))
    pcm = _wav_to_slin_plain(buf.getvalue())
    assert len(pcm) == 800 * 2 and set(struct.unpack("<800h", pcm)) == {1000}


def test_something_that_is_not_a_wav_gives_no_audio_rather_than_noise():
    assert _wav_to_slin_plain(b"MOCK[en]:hello") == b""


# ---- hearing speech ------------------------------------------------------------------------------------------------------------------


def listen(frames):
    ep, out = Endpointer(), []
    for f in frames:
        u = ep.feed(f)
        if u:
            out.append(u)
    return out


def test_speech_ends_after_the_pause_and_keeps_a_little_either_side():
    (u,) = listen([QUIET] * 30 + [loud()] * 50 + [QUIET] * 60)
    frames = len(u.pcm) // FRAME_BYTES
    assert 50 + 8 <= frames <= 50 + 10 + 8  # the speech, up to 200 ms before it and 160 ms after
    assert u.voiced_ms == 50 * 20


def test_a_pause_inside_a_sentence_does_not_end_it():
    (u,) = listen([loud()] * 25 + [QUIET] * 20 + [loud()] * 25 + [QUIET] * 60)  # 400 ms of quiet is shorter than the 800 ms that ends speech
    assert u.voiced_ms == 50 * 20


def test_two_sentences_are_two_utterances():
    assert len(listen([loud()] * 30 + [QUIET] * 60 + [loud()] * 30 + [QUIET] * 60)) == 2


def test_a_click_is_not_speech():
    assert listen([QUIET] * 20 + [loud()] * 4 + [QUIET] * 60) == []


def test_a_noisy_line_raises_the_bar():
    ep = Endpointer()
    for _ in range(300):
        ep.feed(loud(250))  # steady noise, below the fixed level
    assert ep.floor > 100 and ep.threshold > ep.voice_rms  # the level to clear has risen above the fixed one


def test_a_very_long_turn_is_cut():
    ep = Endpointer(max_ms=1000)
    got = [u for f in [loud()] * 80 for u in [ep.feed(f)] if u]
    assert len(got) == 1 and len(got[0].pcm) // FRAME_BYTES == 50


def test_frame_loudness():
    assert frame_rms(QUIET) == 0 and round(frame_rms(loud(1000))) == 1000


# ---- the agent -----------------------------------------------------------------------------------------------------------------------


@pytest.fixture
def fast(monkeypatch):
    """Time compressed: audio is paced at a few hundred times real speed, so a call that would take a minute takes a moment."""
    monkeypatch.setattr(agent_mod, "FRAME_SECONDS", 0.0002)
    monkeypatch.setattr(agent_mod, "LEAD_SECONDS", 0.0)


def settings(tmp_path, **more):
    return Settings(database_url="sqlite://", storage_dir=str(tmp_path), at_callback_secret="test-secret", public_base_url="http://test", debug_headers=True,
                    scheduler_enabled=False, gateway_number="+2348000009999", verify_random_rate=0.0, enabled_languages="en", sip_port=0, sip_silence_seconds=0.6,
                    asr_url="inprocess://mock", tts_url="inprocess://mock", tts_provider="yarngpt", asr_provider="gpu", _env_file=None, **more)


class Scripted:
    """Stands in for the speech model: each time it is asked it returns the next sentence."""

    def __init__(self, *lines):
        self.lines, self.heard = list(lines), []

    async def transcribe(self, audio, language):
        self.heard.append(len(audio))
        return ASRResult(text=self.lines.pop(0) if self.lines else "", confidence=-0.2, model="scripted", language=language)


class Line:
    """The Asterisk end: connects like AudioSocket does, says what it is told to, and records what Talk sent."""

    def __init__(self, port, number=CALLER):
        self.port, self.number, self.frames, self.hung_up = port, number, [], False

    async def open(self):
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", self.port)
        self.writer.write(pack(ID, uuid.UUID(uuid_for_number(self.number)).bytes))
        self.task = asyncio.ensure_future(self._read())

    async def _read(self):
        while (frame := await read_frame(self.reader)) is not None:
            if frame[0] == HANGUP:
                self.hung_up = True
                break
            if frame[0] == AUDIO:
                self.frames.append(frame[1])

    async def talk_over(self, frames):
        """Silence on the line until Talk has stopped sending, then the caller's frames."""
        while True:
            n = len(self.frames)
            await asyncio.sleep(0.15)
            if len(self.frames) == n and n:
                break
        for f in frames:
            self.writer.write(pack(AUDIO, f))
        await self.writer.drain()

    async def wait_for_audio(self, more_than):
        for _ in range(200):
            if len(self.frames) > more_than:
                return
            await asyncio.sleep(0.05)
        raise AssertionError("Talk sent no audio")


def run_with_server(tmp_path, scenario, asr=None):
    app = create_app(settings(tmp_path))
    seed(app.state.svc.session_factory)
    svc = app.state.svc
    if asr:
        svc.asr = asr

    async def go():
        server = SipServer(svc)
        await server.start()
        try:
            return await asyncio.wait_for(scenario(svc, server.server.sockets[0].getsockname()[1]), 60)
        finally:
            server.stop()

    return svc, asyncio.run(go())


def test_a_caller_is_greeted_heard_and_answered(tmp_path, fast):
    asr = Scripted("Can you look something up for me")

    async def scenario(svc, port):
        line = Line(port)
        await line.open()
        await line.wait_for_audio(0)
        await line.talk_over([QUIET] * 5 + [loud()] * 40 + [QUIET] * 60)
        greeting = len(line.frames)
        await line.wait_for_audio(greeting + 10)  # a reply followed
        line.writer.write(pack(HANGUP))
        await line.writer.drain()
        await asyncio.sleep(0.3)
        return greeting

    svc, greeting = run_with_server(tmp_path, scenario, asr)
    assert greeting > 20  # the greeting was sent as audio
    with svc.session_factory() as db:
        call = db.scalar(select(Call))
        turn = db.scalar(select(CallTurn))
        assert call.from_number == CALLER and call.ended_at is not None  # the call is recorded and closed when the caller hangs up
        assert turn.transcript == "Can you look something up for me" and turn.asr_model == "scripted"
        assert turn.audio_path and open(turn.audio_path, "rb").read(4) == b"RIFF"  # what the caller said was kept as a 16 kHz recording
        assert turn.reply_text


def test_a_hidden_number_is_turned_away_without_a_call_record(tmp_path):
    async def scenario(svc, port):
        line = Line(port, number="")
        await line.open()
        await asyncio.wait_for(line.task, 5)
        return line.hung_up

    svc, hung_up = run_with_server(tmp_path, scenario)
    assert hung_up
    with svc.session_factory() as db:
        assert db.scalar(select(Call)) is None


def test_silence_after_the_greeting_gets_the_silence_reply(tmp_path, fast):
    async def scenario(svc, port):
        line = Line(port)
        await line.open()
        await line.wait_for_audio(0)
        await line.talk_over([QUIET] * 5)
        n = len(line.frames)
        await line.wait_for_audio(n + 5)  # nobody spoke, so Talk spoke again
        return True

    svc, _ = run_with_server(tmp_path, scenario)
    with svc.session_factory() as db:
        assert db.scalar(select(CallTurn)).transcript in (None, "")


def stub_call(**more):
    """A call with no connection behind it, to try one part at a time."""
    return StreamedCall(SimpleNamespace(settings=Settings(_env_file=None, **more)), None, None)


def test_the_caller_talking_over_talk_stops_it():
    async def go():
        async def serve(reader, writer):
            call = StreamedCall(SimpleNamespace(settings=Settings(_env_file=None, sip_barge_ms=200)), reader, writer)
            pump = asyncio.ensure_future(call.pump())
            result["finished"] = await call.play(b"\x00\x01" * 8000 * 6, barge=True)  # six seconds of Talk
            pump.cancel()
            done.set()

        result, done = {}, asyncio.Event()
        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
        await asyncio.sleep(0.1)  # Talk is already speaking when the caller starts
        frames = 0
        loop = asyncio.get_running_loop()
        started = loop.time()
        while not done.is_set():
            writer.write(pack(AUDIO, loud()))
            await writer.drain()
            frames += 1
            await asyncio.sleep(0.02)
            if loop.time() - started > 8:
                break
        server.close()
        return result, loop.time() - started

    result, took = asyncio.run(go())
    assert result["finished"] is False and took < 2  # stopped soon after 200 ms of speech, not after six seconds


def test_talk_is_not_stopped_when_barge_in_is_off():
    async def go():
        async def serve(reader, writer):
            call = StreamedCall(SimpleNamespace(settings=Settings(_env_file=None, sip_barge_in=False, sip_barge_ms=40)), reader, writer)
            pump = asyncio.ensure_future(call.pump())
            result["finished"] = await call.play(b"\x00\x01" * 800, barge=True)  # 100 ms
            pump.cancel()
            done.set()

        result, done = {}, asyncio.Event()
        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
        for _ in range(10):
            writer.write(pack(AUDIO, loud()))
        await asyncio.wait_for(done.wait(), 5)
        server.close()
        return result

    assert asyncio.run(go())["finished"] is True


def test_keypad_digits_are_collected_until_the_count_is_reached():
    async def go():
        call = stub_call()
        call.keys.extend("12")
        task = asyncio.ensure_future(call.hear_digits(4))
        await asyncio.sleep(0.05)
        call.keys.extend("34")
        call.wake.set()
        return await asyncio.wait_for(task, 2)

    assert asyncio.run(go()) == ("digits", "1234")


def test_the_hash_key_ends_the_digits_early_and_other_keys_are_ignored():
    async def go():
        call = stub_call()
        call.keys.extend("1*2#")
        return await call.hear_digits(6)

    assert asyncio.run(go()) == ("digits", "12")


def test_digits_that_do_not_come_end_the_wait_with_what_was_typed():
    async def go():
        call = stub_call(digits_timeout_seconds=0)
        call.keys.extend("9")
        return await call.hear_digits(4)

    assert asyncio.run(go()) == ("digits", "9")


def test_speaking_instead_of_typing_is_reported_as_speech():
    async def go():
        call = stub_call()
        call.utts.append("heard")
        return await call.hear_digits(4)

    assert asyncio.run(go()) == ("speech", "heard")


def test_speech_under_way_when_the_line_goes_silent_is_ended_by_finish():
    ep = Endpointer()
    for f in [QUIET] * 5 + [loud()] * 30:
        assert ep.feed(f) is None
    u = ep.finish()
    assert u is not None and u.voiced_ms == 30 * 20 and not ep.speaking
    assert ep.finish() is None  # nothing left to end


def test_a_trunk_that_stops_sending_audio_when_the_caller_is_quiet_does_not_leave_speech_open(tmp_path):
    """No frames at all after the last word: the clock, not the frames, ends the speech."""
    async def go():
        async def serve(reader, writer):
            call = StreamedCall(SimpleNamespace(settings=Settings(_env_file=None, sip_end_silence_ms=200)), reader, writer)
            tasks = [asyncio.ensure_future(call.pump()), asyncio.ensure_future(call.watch())]
            got["heard"] = await call.wait_for(lambda: bool(call.utts), 3)
            for t in tasks:
                t.cancel()
            done.set()

        got, done = {}, asyncio.Event()
        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        _, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
        for f in [QUIET] * 5 + [loud()] * 30:
            writer.write(pack(AUDIO, f))
        await writer.drain()  # and then nothing more is sent
        await asyncio.wait_for(done.wait(), 5)
        server.close()
        return got

    assert asyncio.run(go())["heard"] is True


# ---- speaking a reply a sentence at a time ----------------------------------------------------------------------------------------------


def audio_service(tmp_path):
    from sofa.audio import AudioService
    return AudioService(Settings(_env_file=None, storage_dir=str(tmp_path)), tts=None)


LONG = ("The nearest place is Yaba Lakeview Clinic, a private clinic on Herbert Macaulay Way, less than a kilometre away. It is open until 6 in the evening. "
        "About 2 people are waiting, so expect around 20 minutes. A consultation costs about 8,000 naira. Shall I book it?")


def test_a_long_reply_is_cut_into_sentences_and_a_price_is_not_cut(tmp_path):
    parts = audio_service(tmp_path).pieces(LONG, "en")
    assert len(parts) >= 4 and parts[0].startswith("The nearest place is Yaba Lakeview Clinic")
    assert any("8,000 naira" in p for p in parts)  # the comma inside a number is not a place to cut
    assert " ".join(parts) == LONG


def test_a_short_reply_stays_whole_and_so_does_a_phrase_already_voiced(tmp_path):
    svc = audio_service(tmp_path)
    assert svc.pieces("Done. Your booking code is K K S T.", "en") == ["Done. Your booking code is K K S T."]
    (svc.out_dir / f"{svc.key(LONG, 'en')}.wav").write_bytes(b"RIFF")  # the whole of it is stored
    assert svc.pieces(LONG, "en") == [LONG]


def test_a_stand_in_voice_does_not_count_as_voiced(tmp_path):
    svc = audio_service(tmp_path)
    path = svc.out_dir / f"{svc.key(LONG, 'en')}.wav"
    path.write_bytes(b"RIFF")
    path.with_suffix(".fallback").write_text("1")
    assert not svc.is_ready(LONG, "en")


class Sink:
    """The other end of a call: collects what is written to it."""

    def __init__(self):
        self.sent = []

    def write(self, data):
        self.sent.append(data)

    async def drain(self):
        pass

    def close(self):
        pass


class StubVoice:
    """Voices a sentence after the time it is told, so the order and the waiting can be tried."""

    def __init__(self, delays=None, fail=()):
        self.delays, self.fail, self.made = delays or {}, fail, []

    def pieces(self, text, lang):
        return [p for p in text.split("|")]

    async def speak_telephone(self, text, lang):
        await asyncio.sleep(self.delays.get(text, 0))
        self.made.append(text)
        return b"\x00\x01" * 160 * (2 if text == "second" else 1)


def streamed(voice, **more):
    call = StreamedCall(SimpleNamespace(settings=Settings(_env_file=None, **more), audio=voice, sessions=None), None, Sink())
    call.writer = Sink()
    return call


def test_sentences_are_spoken_in_order_even_when_a_later_one_is_ready_first(fast):
    voice = StubVoice({"first": 0.15, "second": 0.0})
    call = streamed(voice)
    outcome = SimpleNamespace(extra={}, action="chat", also=[])
    assert asyncio.run(call.say_stream([("first|second", "en")], outcome, barge=False)) is True
    frames = [f for f in call.writer.sent]
    assert len(frames) == 3 and frames[0][3:5] == b"\x00\x01" and voice.made == ["second", "first"]  # made out of order, spoken in order: one frame, then two


def test_a_worded_reply_that_is_too_slow_to_voice_is_replaced_by_its_template(fast):
    voice = StubVoice({"slow worded reply": 5})
    call = streamed(voice, tts_budget_seconds=0.1)
    outcome = SimpleNamespace(extra={"template": ("template wording", "en", None)}, action="chat|generated", also=[], text="slow worded reply", lang="en", parts=None)
    assert asyncio.run(call.say_stream([("slow worded reply", "en")], outcome, barge=False)) is True
    assert voice.made == ["template wording"] and outcome.action == "chat|template_fallback"


def test_the_caller_talking_over_a_reply_stops_the_remaining_sentences(fast):
    voice = StubVoice()
    call = streamed(voice)
    call.s.sip_barge_in = True
    call.barge.set()
    original = call.play

    async def interrupted(pcm, barge=False):  # the caller starts talking as the first sentence begins
        call.playing = True
        return False

    call.play = interrupted
    outcome = SimpleNamespace(extra={}, action="chat", also=[])
    assert asyncio.run(call.say_stream([("a|b|c", "en")], outcome, barge=True)) is False
    assert len(voice.made) <= 3  # whatever was already being made is dropped, nothing further is spoken


def test_noise_already_on_the_line_when_talk_starts_does_not_stop_it():
    """A noisy line reads as speech all the time. If that stopped Talk, every reply would be cut off at once."""
    async def go():
        async def serve(reader, writer):
            call = StreamedCall(SimpleNamespace(settings=Settings(_env_file=None, sip_barge_ms=200)), reader, writer)
            pump = asyncio.ensure_future(call.pump())
            await asyncio.sleep(0.3)  # the noise has been there for a while: the line is already "speaking"
            result["finished"] = await call.play(bytes(2) * 8000, barge=True)  # one second
            pump.cancel()
            done.set()

        result, done = {}, asyncio.Event()
        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
        for _ in range(400):  # steady loud noise, well above the bar, from the start and all the way through
            writer.write(pack(AUDIO, loud()))
            await writer.drain()
            await asyncio.sleep(0.005)
            if done.is_set():
                break
        await asyncio.wait_for(done.wait(), 5)
        server.close()
        return result

    assert asyncio.run(go())["finished"] is True


def test_a_steady_noise_stops_being_speech_as_the_line_level_is_learned():
    ep = Endpointer()
    for _ in range(600):  # 12 seconds of loud steady noise (level 700)
        ep.feed(loud(700))
    assert ep.threshold > 700  # the bar has risen above the noise, so it is no longer "loud"
    assert ep.threshold <= ep.voice_rms * 3 + 1  # but never beyond three times the fixed level: speech must still clear it
    assert listen([loud(700)] * 30 + [loud(3000)] * 30 + [loud(700)] * 60)  # and real speech over that noise is still heard
