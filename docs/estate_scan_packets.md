# Estate scan packets (ground truth for HOSPES adapter map) — 2026-07-13

## GitHub scan (confidence 0.92)

| repo | what it does | reuse fit |
|---|---|---|
| organvm/social-automation | POSSE multi-channel distribution, scheduling, delivery monitoring, analytics (Mastodon, Discord) | episode scheduling + social broadcast |
| organvm/media-ark → primary local 4444J99/media-ark | local-first media archive: ingest→sha256 dedup→OCR→canonical indexing; HTTP API + MCP + dashboard; Free/Pro + MONETA licence; CI green | episode/clip/transcript archive |
| organvm/vox | voice clone/synth/transcribe (ElevenLabs/Coqui/edge-tts, Whisper), FastAPI | transcription; intro/outro synth |
| organvm/salon-archive | transcription pipeline, session metadata, topic taxonomy, search | episode metadata + transcript search |
| organvm/universal-mail--automation (local ~/Workspace/universal-mail--automation) | Gmail/Outlook/iCloud/Mail.app triage; classification; voice-matched reply drafts; DRAFT-ONLY, never sends; escalation; /v1/ops/* | outreach triage + draft engine |
| organvm/content-engine--asset-amplifier | one hero video → 30+ platform-optimized clips/stills/captions + distribution | THE "content cannibalizer" (user's term) |
| organvm/sign-signal--voice-synth | dialogue sequencer over VOX (speech-score family) | audio composition/rehearsal |
| organvm/a-i-council--coliseum | live-streaming platform, live chat, voting, Three.js | THE "streaming app" (user's term) |
| organvm/community-hub | FastAPI portal: archive browse, transcripts, profiles, live rooms, Atom feeds | show hub/site, guest directory, RSS |
| organvm/kerygma-pipeline | poll → template → render → QC → dispatch → analytics orchestrator | publish pipeline pattern |
| 4444J99(+organvm)/application-pipeline (local ~/Code/application-pipeline) | provider-adapter conversion pipeline (greenhouse/lever/ashby/email/browser); Playwright submit; YAML state | guest-opportunity engine (per chat audit) |
| organvm/mirror-mirror (local ~/Workspace/mirror-mirror) | GitHub desc: "private analytics and customer insights platform"; local: React 19 consultation app w/ appointment booking + stylist dashboard | scheduling-concierge claim (per chat audit) — VERIFY actual scheduling code before adapter |

Also local: in-my-head (VOX consumer → 9:16 clip export), speech-score-engine (~/Code/speech-score-engine, live at speech-score-engine.pages.dev), Carrier-Wave media organ (limen/organs/media: drafts-only outbound, system-audio recorder, social_scheduler.py + ffmpeg presets — 5 levers).

## Gaps (both scans agree — matches chat interstitial)
- NO purpose-built guest CRM / booking front office → HOSPES builds it.
- No dedicated podcast repo; capabilities are modular organs → compose through adapters, never merge.

## Naming / home / policy (registry scan, confidence 0.95)
- Name: HOSPES (nomenclator canon-clean; no GitHub/local/roll collision).
- Home: organvm/hospes (standalone revenue product precedent: manumissio, media-ark class).
- Visibility: PRIVATE at creation; awaiting_publish:true in limen positioning seeds; publish = his lever.
