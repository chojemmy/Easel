# BGM library

Easel uses `D:/Easel/assets/music-library` as its persistent, user-owned BGM
library. User media remains outside Git; `library.json` records local provenance,
duration, checksum, tags, and the configured fallback site.

## Migration result (2026-09-28)

The MP3 scan found five paths in the old WorkBuddy checkout, and matching copies
exist under `D:/Easel`. Hash-based deduplication showed three unique recordings:

- one BGM: `mixkit-pop-05-695.mp3` (154.105261 seconds);
- one 66.64-second narration recording;
- one 5.12-second voice test.

Only the actual BGM was added to the music library. Narration and voice tests
stay with their original project outputs so an Agent cannot select them as music.

## Selection policy

1. Select from the local library first (`MUSIC_PROVIDER=local-library`).
2. If no track matches the video's meaning and rhythm, browse
   <https://mixkit.co/free-stock-music/>, verify the current item license, then
   add the download and its metadata to the library.
3. Use a paid AI music provider only when the user explicitly requests original
   or AI-generated music.

The local selection path makes no network or paid API call. It ranks tracks from
`library.json` metadata, copies the chosen file into the video's output folder,
and leaves the library source untouched.
