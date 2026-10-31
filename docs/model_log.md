# Model log: what we measured, what we changed, what it did

The Meta models and the live and test calls of the health build.

## 1. Meta models and live calls (the Lagos health build)

Muse Spark 1.3 reads the request, Muse Voice Transcribe hears the caller, a hosted voice speaks. Measured on the first live and test calls of 28 October.

| # | Finding | Change | Effect |
|---|---|---|---|
| 1.1 | Muse Voice Transcribe in push-to-talk mode returned empty text for a recording sent as a file, and a keyword longer than 30 characters was refused (HTTP 400). | Endpointing mode; keywords shortened to 30 characters (facility names shortened, the rest dropped). | Recordings are read; place and drug names are biased. |
| 1.2 | Muse Spark 1.2 with minimal reasoning padded its JSON with spaces until it ran out of tokens; the token limit also counts the model's thinking. | Muse Spark 1.3, and 800 tokens of room on top of the reply's own limit. | Strict JSON turns complete. |
| 1.3 | First live call: "database is locked" (a turn held a write open through a slow voice and model call), and a recording address that answered 404 for 12.9 s of retries. | Write-ahead logging with a 30 s wait, the first write of a turn saved at once, a provider call saved before the next slow step, shorter retries for a recording. | No more lock errors on calls; a recording is fetched in about a second. |
| 1.4 | The hosted voice refused many requests at once (HTTP 429), and the first warm-up kept 38 silent stand-in clips as the phrases' permanent audio. | Retries after growing pauses, a stand-in kept for two minutes only, a warm-up paced at its rate limit. | 138 fixed phrases voiced properly. |
| 1.5 | The hosted voice ran out of credit (HTTP 402 "no credits left"): every reply that was not a stored phrase came out as silence, only the holding messages were heard. | A voice that runs on the machine itself (Piper) speaks when the hosted one fails, and after a refusal for credit or a key the hosted voice is left alone for ten minutes, so no sentence waits for a failure first. | A reply is always spoken. Credit is still the first thing to check when replies go quiet. |
| 1.6 | First timing of the streamed path (a test call through Asterisk): speech read 2.6 s, understanding and acting 2.9 s, voice 5.5 s because the whole 60-word reply was voiced before any of it played; 11 s in all. | The reply is cut into sentences, voiced side by side and spoken in order, so Talk starts speaking when the first sentence is made. | First sound no longer waits for the last sentence. Real trunk calls are not measured yet. |
| 1.7 | A trunk can stop sending audio while the caller is quiet, so "speech has ended" never fired; and steady line noise above the speech level read as the caller talking over Talk, which would cut every reply off at once. | The end of speech is also timed by the clock; the noise level is learned continuously (never raising the bar above three times the base level); the caller can stop Talk only by starting to speak after Talk began. | Covered by tests; to be confirmed on a real phone line. |
| 1.8 | The health page named the GPU host's voice address, which read as "the voice is a stand-in" while the hosted voice was the one speaking (or, once its credit ended, failing). | The page names the hosted voice and whether a voice on the machine backs it up. | The voice a caller hears can be read off the health page. |
| 1.9 | A recording's address arrives in the telephony callback and was downloaded from wherever it pointed. | Recordings come only over https from the provider's own hosts, and a redirect off them is not followed; recordings are deleted after 30 days. | An address in a callback can no longer make the server fetch an internal page. |

## Open improvements, in order of value

1. Realtime transcription over Meta's WebSocket while the caller speaks (`wss://api.meta.ai/v1/asr/realtime`), so the words are ready when the caller stops: about 2.5 s less per turn than reading a finished recording.
