# Edge TTS Voice Sampler

A small interactive Python program for comparing 13 US English voices with your own sentence. Pick a voice, listen to the sample, and switch voices or change the sentence whenever you like.

## Requirements

- Python 3.10 or later
- The packages listed in `requirements.txt`
- An internet connection to generate speech
- `ffplay` (included with FFmpeg) for automatic playback

Install the Python dependency from PowerShell:

```powershell
py -m pip install -r requirements.txt
```

For automatic playback, install FFmpeg and make sure `ffplay` is available on your `PATH`. Without `ffplay`, the program still saves MP3 files, which you can open in another audio player.

## Run

From the folder containing `voice_samples.py`:

```powershell
py voice_samples.py
```

Type a sentence when prompted, or press **Enter** to use the default: “Hello! It's a lovely day to learn something new.”

You can also start with a sentence directly:

```powershell
py voice_samples.py --text "Welcome! Choose a voice to hear me say this."
```

See all options with:

```powershell
py voice_samples.py --help
```

## Use the menu

At the voice menu:

- Enter a number from **1–13** to generate and play that voice.
- Enter **t** to type a different sentence.
- Enter **q** to quit.

The menu returns after each sample, so you can compare voices with the same sentence. Generated MP3 files are saved in the `voice_samples` folder by default. A voice's file is replaced the next time you generate a sample with that voice. Failed generations leave the last successful sample untouched.

To save files somewhere else, pass an output folder:

```powershell
py voice_samples.py --output-dir "my samples"
```

## Retries and diagnostics

Each selected voice is tried up to **3 times** by default. Failed requests are retried after a short increasing delay (1 second, then 2 seconds). Each try has a **30-second timeout**. Change these settings if needed:

```powershell
py voice_samples.py --attempts 5 --attempt-timeout 60
```

For detailed, timed WebSocket message information—including on successful requests—use `--debug`:

```powershell
py voice_samples.py --debug
```

On a failed try, the program always prints the exception, the WebSocket message summary, audio byte count, and a short diagnosis. The detailed trace reports message types, timing, audio frame sizes, and connection close codes; it does not print your sentence, SSML, or audio contents.

The message `NoAudioReceived` means the Edge speech service ended a request without sending audio. It can be a temporary service or connection issue; a retry may work. If it only happens for one voice or sentence, the service may be rejecting that particular voice/request. Increasing the timeout helps only when a request is slow; it cannot fix a response that has already ended without audio.

Run the local test suite with:

```powershell
py -m unittest discover -s tests -v
```

## Voices

| # | Voice | Gender | Style / description |
|---:|---|---|---|
| 1 | `en-US-AnaNeural` | Female | Cartoon, conversation; cute |
| 2 | `en-US-AndrewNeural` | Male | Conversation, Copilot; warm, confident, authentic, honest |
| 3 | `en-US-AriaNeural` | Female | News, novel; positive, confident |
| 4 | `en-US-AvaNeural` | Female | Conversation, Copilot; expressive, caring, pleasant, friendly |
| 5 | `en-US-BrianNeural` | Male | Conversation, Copilot; approachable, casual, sincere |
| 6 | `en-US-ChristopherNeural` | Male | News, novel; reliable, authoritative |
| 7 | `en-US-EmmaNeural` | Female | Conversation, Copilot; cheerful, clear, conversational |
| 8 | `en-US-EricNeural` | Male | News, novel; rational |
| 9 | `en-US-GuyNeural` | Male | News, novel; passionate |
| 10 | `en-US-JennyNeural` | Female | General; friendly, considerate, comforting |
| 11 | `en-US-MichelleNeural` | Female | News, novel; friendly, pleasant |
| 12 | `en-US-RogerNeural` | Male | News, novel; lively |
| 13 | `en-US-SteffanNeural` | Male | News, novel; rational |

## Troubleshooting

- **`edge_tts` is missing:** install it with `py -m pip install edge-tts`.
- **The MP3 is generated but does not play:** install FFmpeg and ensure `ffplay` is on your `PATH`, or open the MP3 from the output folder manually.
- **`NoAudioReceived` or another speech-generation error:** the program retries transient failures automatically. If retries are exhausted, check your internet connection and try again later. Use `--debug` to inspect the WebSocket message sequence.
- **A voice consistently fails for only certain text:** try a short, plain sentence and compare with another voice; the Edge service may reject a particular text/voice combination.
