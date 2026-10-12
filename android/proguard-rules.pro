# 纯净电视 R8 收缩规则（build.sh 步骤 3 使用）
# 目标：只保留真正用到的类 —— media3/exo2/androidx/guava/kotlin 全量 jar 约 18MB，
#       d8 全量 dex 出来 13.2MB；R8 树摇后预计砍掉一大半。

# 应用自身代码全保留（含反射点 Class.forName("com.clean.tv.Media3PlayerBackend")
# 与 @JavascriptInterface 桥；app 包很小，全 keep 几乎无收缩损失）
-keep class com.clean.tv.** { *; }

# 播放链依赖保守 keep（电视盒子兼容优先：media3/exo2 内部有少量反射与
# 资源查找，先全保留，只摇 androidx/guava/kotlin 的冗余部分）
-keep class com.google.android.exoplayer2.** { *; }
-keep class androidx.media3.** { *; }
-keep class com.google.android.material.** { *; }

# legacy multidex（min-api 19 走 MultiDexApplication）
-keep class androidx.multidex.** { *; }

# JS 桥暴露的方法名不能被混淆/删除（反射调用来路是网页 JS）
-keepclassmembers class * {
    @android.webkit.JavascriptInterface <methods>;
}

# ★ 关键：保留运行时注解。R8 默认会剥掉注解 —— @JavascriptInterface 被剥后
#   WebView 不再暴露桥方法（对象在、方法全没），JS 调用静默 TypeError，
#   实测症状：本机源全部线路失败 + 「当前环境不支持官网页面播放」错误卡。
-keepattributes *Annotation*

# 开发期不做混淆改名：保留堆栈可读性；后续要再省体积可去掉这行
-dontobfuscate

# 收缩时遇到缺省 stub 告警一律忽略（android.jar 之外的 Optional/AWT 等）
-dontwarn **
