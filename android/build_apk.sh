#!/usr/bin/env bash
# LAN Drop Android 构建脚本（不依赖 Gradle / Android Studio）
# 说明：aapt2 对中文路径支持不佳，因此在 ASCII 临时目录中构建，产物拷回项目 dist/
set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"
TOOLS="$ROOT/.tools"
SDK="${ANDROID_SDK_ROOT:-$TOOLS/sdk}"
BT="$SDK/build-tools/34.0.0"
PLATFORM="$SDK/platforms/android-34"
AJ="$PLATFORM/android.jar"
JDK="$TOOLS/jdk"
JAVAC="$JDK/bin/javac.exe"
KEYTOOL="$JDK/bin/keytool.exe"
export JAVA_HOME="$JDK"
export PATH="$JDK/bin:$PATH"

WORK="/c/Users/Administrator/AppData/Local/Temp/landrop_apk"
DIST="$ROOT/dist"

echo "==> 准备 ASCII 工作目录 $WORK"
rm -rf "$WORK"
mkdir -p "$WORK/classes" "$WORK/dex" "$WORK/gen" "$WORK/src" "$WORK/res" "$WORK/assets"
cp -r "$ROOT/src/." "$WORK/src/"
cp -r "$ROOT/res/." "$WORK/res/"
cp -r "$ROOT/assets/." "$WORK/assets/"
cp "$ROOT/AndroidManifest.xml" "$WORK/AndroidManifest.xml"
mkdir -p "$DIST"

cp "$AJ" "$WORK/android.jar"
cd "$WORK"

echo "==> aapt2 编译资源"
"$BT/aapt2.exe" compile --dir res -o res.zip

echo "==> aapt2 链接资源与清单"
"$BT/aapt2.exe" link \
  -o base.apk \
  -I "$WORK/android.jar" \
  --manifest AndroidManifest.xml \
  -A assets \
  --min-sdk-version 24 --target-sdk-version 34 \
  --version-code 3 --version-name 2.2 \
  --java gen \
  res.zip

echo "==> javac 编译 Java 源码"
find src gen -name "*.java" > sources.txt
"$JAVAC" -encoding UTF-8 -source 8 -target 8 -nowarn \
  -cp "$WORK/android.jar" -d classes @"sources.txt"
if [ ! -d classes/com/landrop ]; then
  echo "❌ Java 编译失败"; exit 1
fi

echo "==> d8 生成 dex"
find classes -name "*.class" > classes.txt
"$BT/d8.bat" --lib "$WORK/android.jar" --min-api 24 --release \
  --output dex @"classes.txt" > /dev/null

echo "==> 组装 APK（写入 classes.dex）"
python - "$WORK/base.apk" "$WORK/dex/classes.dex" "$WORK/unsigned.apk" <<'PY'
import sys, zipfile, shutil
base, dex, out = sys.argv[1], sys.argv[2], sys.argv[3]
shutil.copy(base, out)
with zipfile.ZipFile(out, "a", zipfile.ZIP_DEFLATED) as z:
    z.write(dex, "classes.dex")
print("   classes.dex 写入完成")
PY

echo "==> zipalign 对齐"
"$BT/zipalign.exe" -f 4 unsigned.apk aligned.apk

echo "==> 签名"
KS="$ROOT/landrop.keystore"
if [ ! -f "$KS" ]; then
  "$KEYTOOL" -genkeypair -v -keystore "$KS" -alias landrop \
    -keyalg RSA -keysize 2048 -validity 10000 \
    -storepass landrop -keypass landrop \
    -dname "CN=LAN Drop, OU=LAN, O=LAN Drop, L=Local, S=Local, C=CN" > /dev/null 2>&1
fi
"$BT/apksigner.bat" sign --ks "$KS" --ks-pass pass:landrop --key-pass pass:landrop \
  --v1-signing-enabled true --v2-signing-enabled true \
  --out "$WORK/LANDrop.apk" aligned.apk

echo "==> 校验"
"$BT/apksigner.bat" verify --print-certs "$WORK/LANDrop.apk" | head -4
"$BT/aapt2.exe" dump badging "$WORK/LANDrop.apk" | head -6

cp "$WORK/LANDrop.apk" "$DIST/LANDrop.apk"
ls -lh "$DIST/LANDrop.apk"
echo "✅ APK 构建完成：$DIST/LANDrop.apk"
