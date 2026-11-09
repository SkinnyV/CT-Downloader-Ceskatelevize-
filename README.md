# CT Downloader

**CT Downloader** is a Windows desktop application designed to simplify downloading and archiving content from Czech Television's online streaming platform, [ČT iVysílání](https://www.ceskatelevize.cz/ivysilani/).

Built with Python and a graphical interface, it brings together stream detection, media downloading, metadata extraction, and file organization into a single application.

### Features

- **Simple graphical interface:** Enter programme, episode, or series URLs without using command-line tools.
- **Automated stream detection:** Identifies available video and audio streams and their associated manifests.
- **Video and audio handling:** Supports separate stream processing and FFmpeg-based muxing without unnecessary re-encoding.
- **Metadata extraction:** Retrieves available programme information, including titles, episode details, descriptions, and release years.
- **Subtitle support:** Handles available subtitle tracks.
- **Media organization:** Generates consistent filenames and organizes downloaded content into folders.
- **Thumbnail and metadata integration:** Adds available metadata and artwork to the resulting media files.
- **Dependency management:** Built-in tools for installing and updating required components, including yt-dlp and FFmpeg.
- **Detailed logging:** Provides download progress and diagnostic information.

### Technology

- Python
- Tkinter
- yt-dlp
- FFmpeg / FFprobe
- Bento4 (for experimental DASH processing)

### Project

CT Downloader is developed with a focus on media preservation, convenience, and reducing the technical complexity of archiving online television content.

The application is intended for content that users are authorized to download. DRM-protected content requires users to extract and provide their own key pair for the specific video they wish to download. Support for DRM-protected content is not guaranteed. Several browser extensions are available to help facilitate the key extraction process.
