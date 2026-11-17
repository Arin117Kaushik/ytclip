#!/data/data/com.termux/files/usr/bin/bash
# Installs ytclip on an Android phone, inside the Termux app. Paste this into Termux:
#   curl -fsSL https://raw.githubusercontent.com/Arin117Kaushik/ytclip/main/termux-setup.sh | bash
# Run it again any time to update.
set -e
export DEBIAN_FRONTEND=noninteractive
APT="-y -o Dpkg::Options::=--force-confnew"

echo "> Installing Python, ffmpeg, Node.js and git (a few minutes the first time)..."
pkg update -y
apt-get $APT full-upgrade
apt-get $APT install python ffmpeg nodejs-lts git

echo "> Installing yt-dlp..."
pip install -U -q yt-dlp yt-dlp-ejs
# YouTube makes yt-dlp run some JavaScript. On a phone that is Node.js, which yt-dlp leaves off by default.
mkdir -p ~/.config/yt-dlp
grep -qs -- "--js-runtimes" ~/.config/yt-dlp/config || echo "--js-runtimes node" >> ~/.config/yt-dlp/config

echo "> Getting ytclip..."
if [ -d ~/ytclip/.git ]; then git -C ~/ytclip pull -q --ff-only; else git clone -q --depth 1 https://github.com/Arin117Kaushik/ytclip ~/ytclip; fi

# "ytclip" starts the page. Once a day it also updates yt-dlp and ytclip in the background,
# because YouTube changes often and an old yt-dlp is the usual reason downloads stop working.
cat > "$PREFIX/bin/ytclip" <<'EOF'
#!/data/data/com.termux/files/usr/bin/bash
stamp=~/.cache/ytclip-updated
if [ -z "$(find "$stamp" -mmin -1440 2>/dev/null)" ]; then
  mkdir -p ~/.cache && touch "$stamp"
  (pip install -U -q yt-dlp yt-dlp-ejs; git -C ~/ytclip pull -q --ff-only) >/dev/null 2>&1 &
fi
exec python ~/ytclip/app.py "$@"
EOF
chmod +x "$PREFIX/bin/ytclip"

# Share a YouTube link to Termux and the page opens with it filled in.
mkdir -p ~/bin ~/.shortcuts
printf '#!/data/data/com.termux/files/usr/bin/bash\nexec ytclip "$1"\n' > ~/bin/termux-url-opener
# A home screen button, if the Termux:Widget app is installed.
printf '#!/data/data/com.termux/files/usr/bin/bash\nexec ytclip\n' > ~/.shortcuts/ytclip
chmod +x ~/bin/termux-url-opener ~/.shortcuts/ytclip

echo "> Allow Termux to save into your Downloads folder when Android asks."
[ -d ~/storage/downloads ] || termux-setup-storage

echo
echo "Done. Type: ytclip"
echo "Or share a YouTube video to Termux."
