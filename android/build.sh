#!/usr/bin/env bash
# ============================================================================
# 纯净电视 · 安卓端一键构建（手机 + Android TV 同一个 APK）
# 无需 Android Studio / Gradle，只用 aapt2 + javac + d8 + zipalign + apksigner
#
#   用法：  bash build.sh
#   产出：  纯净电视.apk（自签名，直接安装）
#
#   依赖：  JDK(含 javac/keytool) + tools/ 下已备好的 build-tools 与 android.jar
# ============================================================================
set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$HERE/app"
OUT="$HERE/build/out"
KS="$HERE/ks.jks"
APK="$HERE/纯净电视.apk"

# ---- 工具链位置（可用环境变量覆盖）----
BT="${ANDROID_BUILD_TOOLS:-$HERE/tools/android-14}"
AJAR="${ANDROID_JAR:-$HERE/tools/android-34/android.jar}"
R8="${R8_JAR:-$HERE/tools/r8.jar}"

AAPT2="$BT/aapt2.exe";   [ -x "$AAPT2" ] || AAPT2="$BT/aapt2"
ZIPALIGN="$BT/zipalign.exe"; [ -x "$ZIPALIGN" ] || ZIPALIGN="$BT/zipalign"
APKSIGNER="$BT/apksigner.bat"; [ -f "$APKSIGNER" ] || APKSIGNER="$BT/apksigner"

# ---- 同步共享核心（单一真源在 ../shared/web/tv_core.js）----
cp -f "$HERE/../shared/web/tv_core.js" "$APP/assets/tv_core.js"

for f in "$AAPT2" "$AJAR" "$R8"; do
  [ -e "$f" ] || { echo "缺少构建依赖: $f"; exit 1; }
done

rm -rf "$OUT"; mkdir -p "$OUT"

echo "== 1/5 aapt2 link（资源 + assets + manifest）=="
RES_ARG=""
if [ -d "$APP/res" ]; then
  "$AAPT2" compile --dir "$APP/res" -o "$OUT/res.zip"
  RES_ARG="$OUT/res.zip"
fi
"$AAPT2" link -o "$OUT/base.apk" -I "$AJAR" \
  --manifest "$APP/AndroidManifest.xml" \
  -A "$APP/assets" $RES_ARG \
  --java "$OUT/gen" --auto-add-overlay

echo "== 2/5 javac（Java 源码）=="
mkdir -p "$OUT/classes"
SRCS=$(find "$APP/src" "$OUT/gen" -name "*.java" 2>/dev/null | tr '\n' ' ')
javac -encoding UTF-8 -source 8 -target 8 -nowarn \
  -bootclasspath "$AJAR" -classpath "$AJAR" \
  -d "$OUT/classes" $SRCS 2>&1 | grep -v "警告\|warning\|bootstrap\|系统模块\|source value 8\|target value 8\|已过时\|deprecat" || true

echo "== 3/5 d8（转 dex）=="
mkdir -p "$OUT/dex"
java -cp "$R8" com.android.tools.r8.D8 --lib "$AJAR" --min-api 21 \
  --output "$OUT/dex" $(find "$OUT/classes" -name "*.class")

echo "== 4/5 合并 dex + zipalign =="
cp "$OUT/base.apk" "$OUT/unsigned.apk"
ZIPTOOL=$(command -v 7za || command -v 7z || echo "/d/leidian/LDPlayer9/7za.exe")
(cd "$OUT/dex" && "$ZIPTOOL" a -tzip "$OUT/unsigned.apk" classes.dex >/dev/null)
"$ZIPALIGN" -p -f 4 "$OUT/unsigned.apk" "$OUT/aligned.apk"

echo "== 5/5 签名 =="
if [ ! -f "$KS" ]; then
  keytool -genkeypair -keystore "$KS" -alias clean -keyalg RSA -keysize 2048 \
    -validity 12000 -storepass 123456 -keypass 123456 \
    -dname "CN=CleanTV,OU=Dev,O=Clean,L=NA,ST=NA,C=CN" >/dev/null 2>&1
fi
if [ -f "$APKSIGNER" ]; then
  "$APKSIGNER" sign --ks "$KS" --ks-pass pass:123456 --key-pass pass:123456 \
     --out "$APK" "$OUT/aligned.apk" && echo "apksigner 签名完成"
else
  jarsigner -keystore "$KS" -storepass 123456 -keypass 123456 "$OUT/aligned.apk" clean >/dev/null 2>&1
  cp "$OUT/aligned.apk" "$APK"
fi
echo "== 完成: $APK =="
ls -la "$APK"
