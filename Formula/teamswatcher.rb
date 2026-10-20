class Teamswatcher < Formula
  desc "Auto-records Teams, Zoom and Webex calls and transcribes them locally"
  homepage "https://github.com/pbarcio/homebrew-teamswatcher"
  url "https://github.com/pbarcio/homebrew-teamswatcher/archive/refs/tags/v0.8.2.tar.gz"
  sha256 "84958bd08dcf2497112288c5801824106247e52ec722136959919bce6de83acf"
  license "MIT"
  head "https://github.com/pbarcio/homebrew-teamswatcher.git", branch: "main"

  depends_on arch: :arm64
  depends_on macos: :sonoma
  depends_on "ffmpeg"
  depends_on "uv"

  def install
    system "./build.sh"
    prefix.install "build/TeamsWatcher.app"
    # The bundled ownscribe fork; teamswatcher-setup installs it and builds its audio helper.
    pkgshare.install "ownscribe-config.toml", "ownscribe"

    inreplace "teamswatcher-setup", "@@PREFIX@@", opt_prefix.to_s
    bin.install "teamswatcher-setup"
    bin.install "uninstall.sh" => "teamswatcher-uninstall"
  end

  def caveats
    <<~EOS
      Finish setup with:
        teamswatcher-setup

      It installs the bundled ownscribe fork (with MLX), builds its audio
      helper, writes its config, downloads the models, puts
      TeamsWatcher.app in /Applications (or ~/Applications) and adds it to
      Login Items. Re-run it after every `brew upgrade teamswatcher`.

      To remove everything: teamswatcher-uninstall, then brew uninstall teamswatcher
    EOS
  end

  test do
    assert_predicate prefix/"TeamsWatcher.app/Contents/MacOS/TeamsWatcher", :executable?
    assert_match "teamswatcher-setup", shell_output("#{bin}/teamswatcher-setup --help")
  end
end
