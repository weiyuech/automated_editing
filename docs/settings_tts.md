# Settings And TTS

`SettingsService` is the only backend service that owns provider configuration. The UI can save and test providers through `/api/settings`, but raw secrets are not returned to the renderer.

## Runtime Shape

- `SettingsService`: stores masked/sanitized LLM and TTS provider config.
- `LLMService`: consumes `SettingsService` for script drafting and health checks.
- `TTSService`: consumes `SettingsService` for Volcengine sync TTS with word timestamps.
- `MediaService`: registers generated voiceover audio as normal media.
- `RenderService`: mixes optional background music and optional voiceover separately.

## Generated Voiceover Files

Voiceover files live under `data/tts/`:

- `*.mp3` or `*.wav`: generated voice audio.
- `*.json`: provider metadata, final text, duration, words, and phonemes.

The vault shows the audio asset and counts the metadata in storage. Metadata files are not shown as user media rows.

## Provider Notes

Volcengine fast sync TTS uses:

```json
{
  "request": {
    "operation": "query",
    "with_timestamp": 1
  }
}
```

The useful response field is `addition.frontend`, which is a JSON string containing `words[]` with `start_time` and `end_time` in milliseconds.
