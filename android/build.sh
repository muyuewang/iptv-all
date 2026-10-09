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
M3LIB="$HERE/tools/media3/libs"            # media3 + androidx + guava（fetch_media3.py 产出）
E2LIB="$HERE/tools/exoplayer2/libs"        # 老 ExoPlayer2.19.1 + androidx + guava（fetch_exoplayer2.py 产出）

AAPT2="$BT/aapt2.exe";   [ -x "$AAPT2" ] || AAPT2="$BT/aapt2"
ZIPALIGN="$BT/zipalign.exe"; [ -x "$ZIPALIGN" ] || ZIPALIGN="$BT/zipalign"
APKSIGNER="$BT/apksigner.bat"; [ -f "$APKSIGNER" ] || APKSIGNER="$BT/apksigner"
JAVAC="${JAVAC:-javac}"
D8="${D8:-java -cp $R8 com.android.tools.r8.D8}"

# ---- 收集两套媒体库 classpath ----
# 无 gradle，手工收集全部依赖 jar；Windows 用 ';' 分隔，类 Unix 用 ':'。
# ★ 关键：javac 的 -classpath 是「;」分隔的单个参数，Git Bash/MSYS 不会把
#   其中的 /c/... POSIX 路径自动转成 Windows 路径，javac 就会报「程序包不存在」。
#   所以这里必须用 cygpath -w 显式转成 C:\... 再拼。
collect_cp() {   # $1 = 目录, 输出 Windows 路径 ';' 拼接串
  local dir="$1" out=""
  [ -d "$dir" ] || return 0
  for j in "$dir"/*.jar; do
    [ -f "$j" ] || continue
    local wj=$(cygpath -w "$j" 2>/dev/null || echo "$j")
    if [ -z "$out" ]; then out="$wj"; else out="$out;$wj"; fi
  done
  echo "$out"
}
M3CP=$(collect_cp "$M3LIB")
E2CP=$(collect_cp "$E2LIB")
# javac 的 -classpath 同时要带上 android.jar（已转 Windows 路径）
AJAR_WIN=$(cygpath -w "$AJAR" 2>/dev/null || echo "$AJAR")

# ---- 同步共享核心（单一真源在 ../shared/web/tv_core.js）----
cp -f "$HERE/../shared/web/tv_core.js" "$APP/assets/tv_core.js"

for f in "$AAPT2" "$AJAR" "$R8"; do
  [ -e "$f" ] || { echo "缺少构建依赖: $f"; exit 1; }
done
if [ -z "$M3CP" ]; then
  echo "缺少 media3 依赖：请先运行  python tools/fetch_media3.py"
  exit 1
fi
if [ -z "$E2CP" ]; then
  echo "缺少老 ExoPlayer2 依赖：请先运行  python tools/fetch_exoplayer2.py"
  exit 1
fi

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
set +e
$JAVAC -encoding UTF-8 -source 8 -target 8 -Xlint:-options -nowarn \
  -bootclasspath "$AJAR" -classpath "$AJAR_WIN;$M3CP;$E2CP" \
  -d "$OUT/classes" $SRCS > "$OUT/javac.log" 2>&1
JAVAC_EXIT=$?
set -e
if [ $JAVAC_EXIT -ne 0 ]; then
  echo "javac 失败 (exit=$JAVAC_EXIT)，错误如下："
  # Windows JDK 输出是 GBK，转成 UTF-8 后再打印（失败行含「错误/error」）
  iconv -f GBK -t UTF-8 "$OUT/javac.log" 2>/dev/null | grep -a "错误\|error\|不存在\|找不到" \
    || iconv -f GBK -t UTF-8 "$OUT/javac.log" 2>/dev/null \
    || cat "$OUT/javac.log"
  exit 1
fi

echo "== 3/5 d8（转 dex，多 dex）=="
mkdir -p "$OUT/dex"
# media3 + exoplayer2 + guava 体积大，方法数远超单 dex 65K 上限；
# d8 在 min-api>=21 时会自动切分 classes.dex / classes2.dex …。
# ★ 注意 --min-api 降到 19（兼容 Android 4.4）；multidex 仍由 d8 自动处理，
#   API<21 需要 legacy multidex 支持 —— 见 AndroidManifest 的 androidx.multidex。
#
# ★ 去重：media3 与 exoplayer2 都带 androidx.core / annotation / guava 等传递依赖，
#   版本不同会导致 d8 报「defined multiple times」。处理：
#   - androidx/guava 系列：统一用 media3 目录的（版本更新，向后兼容老 ExoPlayer2）
#   - exoplayer2 目录：只取它独有的 exoplayer-*.jar + multidex.jar
E2_ONLY=$(ls "$E2LIB"/exoplayer-*.jar "$E2LIB"/multidex.jar 2>/dev/null)

$D8 --lib "$AJAR" --min-api 19 \
  --main-dex-list "$HERE/build/maindex.txt" \
  --output "$OUT/dex" \
  $(find "$OUT/classes" -name "*.class") \
  $(ls "$M3LIB"/*.jar) \
  $E2_ONLY

echo "== 4/5 合并 dex + zipalign =="
cp "$OUT/base.apk" "$OUT/unsigned.apk"
ZIPTOOL=$(command -v 7za || command -v 7z || echo "/d/leidian/LDPlayer9/7za.exe")
(cd "$OUT/dex" && "$ZIPTOOL" a -tzip "$OUT/unsigned.apk" classes*.dex >/dev/null)
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
