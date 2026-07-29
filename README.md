# بوت تيليجرام لبناء IPA غير موقّع

يستقبل البوت مشروع SwiftUI/XcodeGen مضغوطًا بصيغة ZIP، يفحصه، ينشئ فرع GitHub مؤقتًا، يشغّل البناء على `macos-latest`، ثم يرسل:

- صورة حقيقية من محاكي iPhone.
- ملف `IPA` غير موقّع.
- بصمة SHA-256 داخل نتيجة GitHub Actions.

> ملف IPA غير الموقّع لا يثبت مباشرة على iPhone. يجب توقيعه لاحقًا بشهادة Apple أو أداة تثبيت مناسبة.

## 1. إنشاء مستودع البناء

أنشئ مستودع GitHub **خاصًا ومخصصًا لهذا البوت فقط**. ارفع إليه كل ملفات هذا المجلد، وتأكد أن الملف التالي موجود على الفرع `main`:

```text
.github/workflows/ipa-builder.yml
```

من إعدادات المستودع افتح **Actions → General** واسمح بتشغيل GitHub Actions.

## 2. إنشاء مفاتيح الوصول

1. أنشئ البوت باستخدام `@BotFather` وخذ `TELEGRAM_BOT_TOKEN`.
2. أنشئ GitHub fine-grained personal access token للمستودع المخصص فقط.
3. امنح الرمز:
   - Contents: Read and write
   - Actions: Read and write
   - Metadata: Read

لا تضع الرموز داخل الملفات ولا ترسلها في محادثة. ضعها في أسرار منصة الاستضافة.

## 3. معرفة Telegram User ID

شغّل البوت أول مرة، ثم أرسل:

```text
/whoami
```

ضع الرقم الناتج داخل `ALLOWED_TELEGRAM_USER_IDS`. يمكن إدخال أكثر من رقم مفصول بفاصلة. إذا تركته فارغًا، يستطيع أي شخص استخدام البوت، وهذا غير موصى به.

## 4. الإعداد

انسخ `.env.example` إلى `.env` واملأ القيم:

```env
TELEGRAM_BOT_TOKEN=...
GITHUB_TOKEN=...
GITHUB_OWNER=...
GITHUB_REPO=...
GITHUB_DEFAULT_BRANCH=main
ALLOWED_TELEGRAM_USER_IDS=123456789
```

حد تنزيل الملفات عبر خوادم Telegram العامة أصغر من حد الإرسال؛ لذلك القيمة الافتراضية لـ `MAX_ZIP_MB` هي 19 MB. يمكن استخدام Bot API Server محلي لاحقًا إذا احتجت ملفات أكبر.

## 5. التشغيل بواسطة Docker

```bash
docker build -t telegram-ipa-builder .
docker run --env-file .env --restart unless-stopped telegram-ipa-builder
```

يمكن نشر الصورة نفسها على Railway أو Render أو VPS يدعم خدمة تعمل باستمرار. البوت يستخدم long polling ولا يحتاج نطاقًا أو webhook.

## 6. الاستخدام

أرسل للبوت ZIP يحتوي على مشروع واحد فقط وفي داخله:

```text
project.yml
Assets.xcassets/
*.swift
```

ينشئ البوت طلبًا مستقلًا وفرعًا مؤقتًا، يحذفه بعد انتهاء البناء، ويرسل النتيجة. ملف البناء يرفض التطبيق إذا لم يجد `Assets.car`، حتى لا يعيد تطبيقًا أبيض بسبب غياب الصور.

## اختبارات محلية

```bash
python -m unittest discover -s tests -v
```

## ملاحظات أمنية

- استخدم مستودع بناء منفصلًا لا يحتوي أسرارًا أو مشاريع أخرى.
- لا تضف أسرارًا إلى GitHub Actions لهذا المستودع.
- أبقِ `ALLOWED_TELEGRAM_USER_IDS` محددًا.
- مشاريع XcodeGen تستطيع تشغيل build scripts؛ لذلك عامل الملفات المرسلة على أنها كود غير موثوق، حتى مع تشغيلها على runner مؤقت.
