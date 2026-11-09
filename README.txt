CT Downloader for Windows — easy setup
======================================
1. Extract the ZIP to a writable folder (e.g. F:\youtubedl\CT_Downloader).
2. Double-click Launch_CT_Downloader.bat.
3. If Python is missing, the launcher offers to install it with winget (Windows
   Package Manager). If winget is unavailable, install Python 3 from python.org.
   Close and reopen the launcher after Python installs.
4. Click 'Install / Update Tools' in the GUI. Confirm the download of yt-dlp.exe
   (GitHub official release), FFmpeg Essentials (gyan.dev), and Bento4 SDK (bok.net). This may take
   time and requires internet access, but does not need administrator rights.
5. Paste a ceskatelevize.cz episode URL, choose output folder and quality,
   then click Download.

There are NO pip dependencies. Tkinter is bundled with normal Python Windows
installations. yt-dlp.exe, ffmpeg.exe, ffprobe.exe, mp4info.exe, mp4dump.exe and
mp4decrypt.exe are installed next to the GUI script and updated only when you click Install / Update Tools.

Notes:
- The 'Install / Update Tools' button downloads executable programs from the
  internet, after your confirmation; use only sources you trust.
- Bento4 Windows SDK 1.6.0-641 is downloaded from the official Bento4 binary host
  (www.bok.net); only mp4info.exe, mp4dump.exe and mp4decrypt.exe are extracted.
- FFmpeg package is large (often over 100 MB).
- The app uses ČT's undocumented stream-data/media/external API to get a fresh
  HLS URL. Changes to the API can break downloading.
- Geo restrictions, DRM and CDN refusal are not bypassed.
- Program has been syntax-checked, but live Windows installation/download
  testing was not possible in this environment.

The episode field shows a grey example URL as a hint until you start typing.

Version 4: Right-click Cut, Copy, Paste and Select All in the URL and output-folder fields.

Version 5: Output filenames use showTitle and episodeTitle from the ČT API,
plus the episode ID. Czech subtitle filenames use the same basename.

Version 6: Best-effort fetch of episode synopsis and production year from episode webpage.
After download, FFmpeg losslessly remuxes those MKV tags and API chapters. If metadata extraction/muxing fails, original download is kept.

Version 7: Fix metadata embedding when yt-dlp writes an MP4. The app now detects
MP4 or MKV, creates a tagged MKV with FFmpeg stream copy (no re-encoding),
and removes the original MP4 only after the MKV was successfully created.
If remuxing fails, the original downloaded file is retained.

VERSION 8: Episode artwork
When the API provides previewImageUrl, the downloader downloads the original
JPEG and attaches it to the final MKV as cover.jpg (image/jpeg), without
recompressing the thumbnail or video. A missing or invalid thumbnail does not
prevent metadata remuxing. Some players do not display MKV cover attachments.

VERSION 9: Subtitles are embedded automatically in the finished MKV.
The downloader retrieves every distinct language/type offered by the ČT API,
preferring WebVTT and falling back to supported TTML. They are muxed as
selectable SRT tracks. None are marked default or forced; playback begins
without subtitles unless the player itself overrides this preference.
Subtitle files are temporary and are not left alongside the video.
Any download or conversion errors are shown in the log. If subtitle muxing
fails, the app retries the tagged MKV without the affected external subtitles.
The original downloaded MP4 is removed only after successful MKV creation.

Version 11: yt-dlp log output uses UTF-8 consistently to display Czech characters.

Version 12: MKV tags include show/album, episode title, publisher, downloader credit, episode ID, source URL, and reliable audio language. Genre, episode number, and original broadcast date are embedded only if explicitly returned by the episode API or structured page metadata. Original broadcast date is not inferred from upload/availability dates.

Version 14: Accepts programme-only URLs (e.g. films), attempts to locate the playable video ID in the page, and validates it against the playback API. Some programme pages may not expose a usable ID.

Version 15: Programme-page ID resolution verifies programme title and approximate duration. Fails safely rather than downloading unrelated recommendations.


VERSION 16: Experimental encrypted DASH acquisition (opt-in)
- Tick 'Experimental: allow encrypted DASH download (not playable)' and acknowledge warning.
- Supports 11-digit standalone external video IDs and ČT stream-data API URLs.
- For programme pages without an embedded video ID, use the Video ID override field.
  Example: the Suburbicon browser request identified 22638149403.
- Uses ČT DRM-capable DASH metadata and yt-dlp --allow-unplayable-formats.
- This is a best-effort attempt only. yt-dlp or the CDN may refuse encrypted formats.
- Saves an ENCRYPTED-named file without key retrieval, decryption, MKV remux, or metadata tagging.
- Does not produce playable output. Only the ordinary unencrypted path creates tagged MKVs.

Version 17: 11-digit IDEC IDs discovered automatically (including DRM mode),
with a supplementary programme-to-episode lookup. Candidates must match
title and duration. If unavailable, the downloader fails safely.

VERSION 18 — experimental manual-key encrypted DASH processing
- In the GUI, check "Encrypted DASH" and enter one KID:KEY pair per line.
- Keys are used only in memory for this session, never written to settings/logs.
- Requires Bento4 mp4info.exe, mp4dump.exe and mp4decrypt.exe in the app
  folder or in PATH, in addition to existing yt-dlp, FFmpeg and ffprobe.
- For the protected stream, the app requests DASH metadata and tries to download
  separate video and audio with yt-dlp. This remains dependent on whether that
  version of yt-dlp supports the CDN's encrypted DASH representation.
- For each track it reads the sample-group encryption KID, selects a supplied
  matching key, invokes mp4decrypt via decimal track ID, checks stream metadata,
  and combines tracks losslessly. It then attempts the existing metadata/subtitle
  tagging workflow. Keys are never retrieved from the DRM license server.
- Encrypted mode with no supplied keys preserves encrypted tracks (v17 behaviour).
- A failed attempt retains downloaded working files in a ctdash_* subdirectory
  to help troubleshoot. On success working media is removed.
- Untested with live ČT CDN/Windows playback in this build. ffprobe validation
  cannot guarantee the output actually decodes; inspect the resulting file.
- Only use content keys and media you are authorized to process.

VERSION 19 — automatic Bento4 installation
- Install / Update Tools now retrieves the official Bento4 Windows SDK along with
  yt-dlp and FFmpeg; mp4info, mp4dump and mp4decrypt are installed beside the app.
- Existing tools are replaced only after each executable has been downloaded and
  validated as a Windows PE executable. Failed installs are logged.


VERSION 20 — encrypted DASH download/decryption workflow correction
- Encrypted mode downloads the video and audio adaptation sets in two separate
  yt-dlp runs. This avoids yt-dlp's comma-as-fallback format syntax and avoids
  merging the encrypted tracks before decryption.
- Video quality is applied only to the video selection; audio uses bestaudio.
- The app then reads each file's sample-group KID, matches the supplied KID:KEY
  pair, decrypts each file by its actual MP4 track ID, and muxes the clear tracks.
- If keys are pasted while Encrypted DASH is unchecked, the app now warns that
  the keys will otherwise be ignored.
- Failed processing keeps the encrypted/intermediate files in a ctdash_* folder
  and logs its location. This workflow still requires keys supplied by the user;
  the app does not retrieve Widevine license keys.
- Syntax and package checks passed; a live ČT/Windows end-to-end test remains
  necessary.
