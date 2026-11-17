#!/usr/bin/env python3
"""Builds the ytclip Android app without Gradle:  python android/build.py

Needs the Android SDK (build-tools and one platform) and a JDK 17 or newer, e.g. the one
inside Android Studio. Writes android/out/ytclip.apk, signed with the standard debug key.
"""
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
EXE = ".exe" if os.name == "nt" else ""
BAT = ".bat" if os.name == "nt" else ""


def newest(folder):
    return sorted(Path(folder).iterdir(), key=lambda p: [int(x) for x in p.name.split("-")[-1].split(".") if x.isdigit()])[-1]


def find_jdk():
    candidates = [os.environ.get("JAVA_HOME"), r"C:\Program Files\Android\Android Studio\jbr",
                  "/Applications/Android Studio.app/Contents/jbr/Contents/Home", "/opt/android-studio/jbr"]
    for home in filter(None, candidates):
        if (Path(home) / "bin" / f"javac{EXE}").is_file():
            return Path(home) / "bin"
    javac = shutil.which("javac")
    if javac:
        return Path(javac).parent
    sys.exit("No JDK found. Install Android Studio or set JAVA_HOME.")


ENV = dict(os.environ)


def run(*cmd):
    print(">", Path(str(cmd[0])).name, *[str(c) for c in cmd[1:3]], "...")
    subprocess.run([str(c) for c in cmd], check=True, env=ENV)


def main():
    sdk = Path(os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
               or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Android" / "Sdk")
    tools = newest(sdk / "build-tools")
    platform = sdk / "platforms" / "android-34"
    android_jar = (platform if platform.is_dir() else newest(sdk / "platforms")) / "android.jar"
    jdk = find_jdk()
    # d8 and apksigner are scripts that look for Java on PATH.
    ENV.update(JAVA_HOME=str(jdk.parent), PATH=str(jdk) + os.pathsep + os.environ["PATH"])

    shutil.rmtree(OUT, ignore_errors=True)
    (OUT / "gen").mkdir(parents=True)
    (OUT / "classes").mkdir()

    run(tools / f"aapt2{EXE}", "compile", "--dir", HERE / "res", "-o", OUT / "res.zip")
    run(tools / f"aapt2{EXE}", "link", "-I", android_jar, "--manifest", HERE / "AndroidManifest.xml",
        "--java", OUT / "gen", "-o", OUT / "unsigned.apk", OUT / "res.zip")

    sources = [*(HERE / "src").rglob("*.java"), *(OUT / "gen").rglob("*.java")]
    run(jdk / f"javac{EXE}", "-source", "17", "-target", "17", "-nowarn", "-encoding", "utf-8",
        "-classpath", android_jar, "-d", OUT / "classes", *sources)
    run(tools / f"d8{BAT}", "--min-api", "24", "--lib", android_jar, "--output", OUT,
        *(OUT / "classes").rglob("*.class"))

    with zipfile.ZipFile(OUT / "unsigned.apk", "a", zipfile.ZIP_DEFLATED) as apk:
        apk.write(OUT / "classes.dex", "classes.dex")
    run(tools / f"zipalign{EXE}", "-f", "4", OUT / "unsigned.apk", OUT / "aligned.apk")

    # The standard Android debug key. Phones only accept updates signed with the same key.
    key = Path.home() / ".android" / "debug.keystore"
    if not key.is_file():
        key.parent.mkdir(exist_ok=True)
        run(jdk / f"keytool{EXE}", "-genkeypair", "-keystore", key, "-storepass", "android", "-alias", "androiddebugkey",
            "-keypass", "android", "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000",
            "-dname", "CN=Android Debug,O=Android,C=US")
    run(tools / f"apksigner{BAT}", "sign", "--ks", key, "--ks-pass", "pass:android",
        "--out", OUT / "ytclip.apk", OUT / "aligned.apk")
    print("Built", OUT / "ytclip.apk")


if __name__ == "__main__":
    main()
