"""The audio warm-up keeps only audio the real voice made, and asks again for a phrase the voice service refused."""

import asyncio

from scripts import warm_cache


class FakeAudio:
    """An audio service whose voice is refused for the first `refusals` tries of each phrase, as a rate-limited one is."""

    def __init__(self, folder, refusals):
        self.folder, self.refusals, self.tries = folder, refusals, {}

    async def speak(self, text, lang):
        n = self.tries[text] = self.tries.get(text, 0) + 1
        path = self.folder / f"{abs(hash(text))}.wav"
        path.write_bytes(b"audio")
        marker = path.with_suffix(".fallback")
        if n <= self.refusals:
            marker.write_text("0")
        else:
            marker.unlink(missing_ok=True)
        return "url", str(path)


def test_a_refused_phrase_is_thrown_away_and_asked_for_again_until_the_real_voice_makes_it(tmp_path):
    audio = FakeAudio(tmp_path, refusals=2)
    assert asyncio.run(warm_cache.speak_properly(audio, "Are you still there?", "en", pace=0, pause=0)) is True
    assert audio.tries["Are you still there?"] == 3 and not list(tmp_path.glob("*.fallback"))


def test_a_phrase_that_is_never_made_is_reported_and_leaves_no_silent_file(tmp_path):
    audio = FakeAudio(tmp_path, refusals=99)
    assert asyncio.run(warm_cache.speak_properly(audio, "Hello", "en", pace=0, pause=0)) is False
    assert audio.tries["Hello"] == warm_cache.RETRIES and not list(tmp_path.glob("*.wav")) and not list(tmp_path.glob("*.fallback"))


def test_a_phrase_made_first_time_is_kept(tmp_path):
    audio = FakeAudio(tmp_path, refusals=0)
    assert asyncio.run(warm_cache.speak_properly(audio, "Hello", "en", pace=0, pause=0)) is True and audio.tries["Hello"] == 1
