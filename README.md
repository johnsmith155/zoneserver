# ZoneVPN — منطق سرور (V2Ray config collector)

این سرویس روی **سرور ایران** اجرا می‌شود و این کارها را انجام می‌دهد:

1. از ریپوزیتوری‌هایی که در `config.json` تعریف می‌کنی، کانفیگ‌های رایگان V2Ray را **لیچ** می‌کند (`vmess`, `vless`, `trojan`, `shadowsocks`).
2. با **xray-core** هر کانفیگ را واقعاً تست می‌کند و **real-delay (پینگ واقعی)** می‌گیرد.
3. فقط کانفیگ‌هایی که **متصل می‌شوند و پینگ مناسب دارند** را نگه می‌دارد.
4. کشور هر کانفیگ را تشخیص می‌دهد، **پرچم کشور** را اول اسم می‌گذارد و اسم را به `🇩🇪 zone-vpn-<random>` تغییر می‌دهد.
5. نتیجه را به‌صورت **JSON مرتب‌شده از کم‌ترین پینگ به بیش‌ترین** در یک **GitHub Gist** ذخیره می‌کند.
6. هر **۱۰ دقیقه** (قابل تنظیم) لیست را به‌روز می‌کند.

اپلیکیشن موبایل بعداً همین Gist را می‌خواند.

---

## ساختار خروجی JSON (که اپ موبایل می‌خواند)

```json
{
  "updated_at": "2026-06-27T19:30:00Z",
  "count": 120,
  "configs": [
    {
      "name": "🇩🇪 zone-vpn-a8f2k",
      "ping": 84,
      "country": "DE",
      "flag": "🇩🇪",
      "protocol": "vless",
      "config": "vless://...#%F0%9F..."
    }
  ],
  "raw": "vless://...\nvmess://..."
}
```

- `configs` از کم‌ترین `ping` به بیش‌ترین مرتب است.
- فیلد `config` همان لینک کامل کانفیگ با اسم جدید (پرچم + zone-vpn) است.
- فیلد `raw` همهٔ لینک‌ها را خط‌به‌خط دارد؛ اگر خواستی می‌توانی مستقیم به‌عنوان subscription هم استفاده‌اش کنی.

### ⚠️ ذخیره‌سازی base64 (پیش‌فرض روشن)
چون `gist_base64: true` است، محتوای Gist **خودِ این JSON نیست**؛ بلکه یک رشتهٔ **base64 از همان JSON فشرده** است تا تابلو نباشد. اپ موبایل باید:

```
base64-decode → UTF-8 → JSON.parse
```

اگر خواستی JSON خام و خوانا ذخیره شود، در `config.json` مقدار `gist_base64` را `false` کن.

**نمونهٔ دیکد در اپ:**

Dart / Flutter:
```dart
import 'dart:convert';
final raw = await http.get(Uri.parse(gistRawUrl)).then((r) => r.body);
final jsonStr = utf8.decode(base64.decode(raw.trim()));
final data = jsonDecode(jsonStr);
final configs = data['configs']; // مرتب‌شده از کم‌ترین پینگ
```

Kotlin (Android):
```kotlin
val raw = URL(gistRawUrl).readText().trim()
val jsonStr = String(android.util.Base64.decode(raw, android.util.Base64.DEFAULT), Charsets.UTF_8)
val data = JSONObject(jsonStr)
val configs = data.getJSONArray("configs")
```

JavaScript:
```js
const raw = (await (await fetch(gistRawUrl)).text()).trim();
const data = JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(raw), c => c.charCodeAt(0))));
```

---

## بخش ۱ — کارهای گیت‌هاب (یک‌بار، روی کامپیوتر خودت)

### ۱) ساخت توکن گیت‌هاب
1. برو به <https://github.com/settings/tokens?type=beta> (Fine-grained token).
2. **Generate new token**.
3. در بخش **Account permissions** فقط دسترسی **Gists → Read and write** را بده.
4. توکن را کپی کن (فقط همان لحظه نشان داده می‌شود). این مقدار **حساس** است.

> نیازی نیست از قبل Gist بسازی؛ ویزارد نصب می‌تواند خودش یک Gist عمومی بسازد. اگر خودت ساختی، فقط **id** آن را بردار (قسمت آخر URL گیست).

### ۲) آپلود کد روی گیت‌هاب (اختیاری، فقط برای انتقال راحت به سرور)
داخل همین پوشه:
```bash
git init
git add .
git commit -m "ZoneVPN server logic"
git branch -M main
git remote add origin https://github.com/<username>/<repo>.git
git push -u origin main
```
> فایل `config.json` به‌خاطر `.gitignore` **هیچ‌وقت** آپلود نمی‌شود؛ پس توکنت لو نمی‌رود. اگر نخواستی از گیت استفاده کنی، می‌توانی کل پوشه را با `scp`/SFTP هم به سرور بفرستی.

---

## بخش ۲ — نصب روی سرور ایران (Ubuntu/Debian)

### ۱) آوردن کد به سرور
یا با گیت:
```bash
sudo apt update && sudo apt install -y git
git clone https://github.com/<username>/<repo>.git zonevpn
cd zonevpn
```
یا با SFTP پوشه را آپلود کن و وارد آن شو.

### ۲) اجرای نصب‌کننده (همه‌چیز خودکار)
```bash
sudo bash install.sh
```
این اسکریپت خودش:
- پایتون و پیش‌نیازها را نصب می‌کند،
- **xray-core** را دانلود می‌کند (`bin/xray`)،
- دیتابیس **GeoIP** کشورها را دانلود می‌کند،
- **ویزارد تنظیمات** را اجرا می‌کند و اطلاعات حساس را از تو می‌پرسد:
  - **GitHub token**
  - **Gist id** (یا می‌سازد)
  - **سورس‌ها** (لینک ریپوزیتوری‌های کانفیگ)
  - فاصلهٔ به‌روزرسانی، حداکثر تعداد خروجی و …
- یک سرویس **systemd** نصب می‌کند که خودکار اجرا و بعد از ری‌استارت/کرش دوباره روشن می‌شود.

### ۳) تست یک سیکل (اختیاری ولی توصیه‌شده)
```bash
./venv/bin/python -m zonevpn --once
```
اگر همه‌چیز درست باشد، گیست پر می‌شود و آدرس خام در انتهای ویزارد چاپ شده است:
```
https://gist.githubusercontent.com/raw/<GIST_ID>/zone-vpn.json
```

### ۴) دستورهای روزمره
```bash
systemctl status zonevpn          # وضعیت
journalctl -u zonevpn -f          # لاگ زنده
sudo ./venv/bin/python setup_wizard.py   # تغییر تنظیمات/سورس‌ها
sudo systemctl restart zonevpn    # اعمال تغییرات
```

---

## تنظیمات (`config.json`)

| کلید | توضیح |
|------|-------|
| `sources` | لیست URL خام ریپوزیتوری‌های کانفیگ |
| `github_token` | توکن گیت‌هاب (حساس) |
| `gist_id` / `gist_filename` | مقصد انتشار |
| `name_prefix` | پیشوند اسم (پیش‌فرض `zone-vpn`) |
| `interval_minutes` | فاصلهٔ به‌روزرسانی (پیش‌فرض ۱۰) |
| `test.tcp_prefilter` | پیش‌فیلتر TCP قبل از xray (پیش‌فرض روشن — حذف سریع سرورهای مرده/فیلتر) |
| `test.tcp_timeout` / `test.tcp_concurrency` | زمان و هم‌زمانی پیش‌فیلتر |
| `test.test_url` | آدرس تست پینگ (پیش‌فرض `cp.cloudflare.com/generate_204`) |
| `test.timeout` | حداکثر زمان هر تست (ثانیه) |
| `test.max_ping` | پینگ بیشتر از این رد می‌شود (ms) |
| `test.batch_size` | تعداد کانفیگ در هر نمونهٔ xray |
| `test.parallel_batches` | چند بَچ هم‌زمان (کمترش = سبک‌تر برای سرور) |
| `test.max_output` | حداکثر تعداد کانفیگ منتشرشده |
| `test.min_per_country` | حداقل کانفیگ تضمینی از هر کشور (برای تنوع، `0` = غیرفعال) |

### چطور لیست ریپوزیتوری‌های خودت را بدهی
هنگام ویزارد، در مرحلهٔ «sources» لینک‌های خام را خط‌به‌خط پِیست کن، یا بعداً مستقیم `config.json` را ویرایش کن و `systemctl restart zonevpn` بزن. لینک باید **raw** باشد، مثل:
```
https://raw.githubusercontent.com/<user>/<repo>/<branch>/<file>.txt
```

---

## نکات عملکرد و دقت

خط لولهٔ پردازش برای حجم بالا بهینه شده (با ۴ سورس فعلی: ~۱۸٬۶۰۰ لینک خام → ~۶٬۲۰۰ سرور یکتا):

1. **حذف تکراری** بلافاصله بعد از لیچ (≈۶۸٪ کاهش).
2. **پیش‌فیلتر TCP:** یک TCP-connect سبک و فوق‌العاده هم‌زمان؛ سرورهای مرده/فیلترشده همین‌جا حذف می‌شوند و **اصلاً وارد xray نمی‌شوند**. مهم‌ترین عامل سبک‌ماندن سرور — مخصوصاً از داخل ایران که IP فیلترشده اساساً TCP وصل نمی‌کند.
3. **تست real-delay با xray به‌صورت بَچ:** هر بَچ (پیش‌فرض ۱۰۰ کانفیگ) داخل **یک** پروسهٔ xray با routing تست می‌شود؛ پس به‌جای هزاران پروسه، فقط چند پروسه هم‌زمان داریم.
4. **GeoIP فقط روی بازماندگان** اجرا می‌شود (ارزان).

### تنظیم برای سرور قوی (پرفورمنس بیشتر)
در `config.json` بخش `test`:
```json
"tcp_concurrency": 512,
"parallel_batches": 8,
"batch_size": 100,
"timeout": 4
```
### تنظیم برای سرور ضعیف (سبک‌تر)
```json
"tcp_concurrency": 128,
"parallel_batches": 2,
"timeout": 6
```
- **GeoIP محلی** است (بدون محدودیت نرخ)؛ اگر دیتابیس نبود، به‌صورت خودکار از `ip-api.com` استفاده می‌شود.
- چون این سرور **در ایران** است، پینگ‌ها دقیقاً همان چیزی است که کاربر ایرانی تجربه می‌کند — همان هدف اصلی.
- اگر دسترسی سرور به گیت‌هاب محدود بود، می‌توانی متغیر محیطی پراکسی بدهی:
  ```bash
  sudo systemctl edit zonevpn
  # و این دو خط را اضافه کن:
  # [Service]
  # Environment=HTTPS_PROXY=socks5://127.0.0.1:PORT
  ```

---

## امنیت
- `config.json` با دسترسی `600` ذخیره می‌شود و در `.gitignore` است؛ توکن لو نمی‌رود.
- توکن فقط دسترسی **gist** دارد، نه چیز دیگر.
- اگر توکن لو رفت، از تنظیمات گیت‌هاب Revoke کن و ویزارد را دوباره اجرا کن.
