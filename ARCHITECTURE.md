# ZoneVPN Server Logic — Architecture

این فایل نقشهٔ پروژه است؛ هدف این است که برای ادامهٔ کار لازم نباشد کل کدبیس دوباره خوانده شود.

## هدف پروژه
سرویسی روی یک سرور لینوکسی (تایید‌شده برای "سرور ایران") که هر `interval_minutes` دقیقه:
1. از چند منبع، لینک‌های vmess/vless/trojan/ss جمع می‌کند،
2. آن‌ها را با xray واقعاً تست می‌کند (تأخیر واقعی، نه فقط TCP)،
3. کشور هر سرور را تشخیص می‌دهد،
4. سریع‌ترین‌ها را انتخاب/تغییر نام می‌دهد،
5. خروجی نهایی را در یک GitHub Gist عمومی منتشر می‌کند تا اپ موبایل آن را بخواند.

## جریان اجرا (یک سیکل)
`zonevpn/__main__.py` → `main_loop()`/`run_single()` → `zonevpn/runner.py::run_cycle()`:

```
sources.collect()        # دانلود لینک‌ها از URLهای config، دیکد ساب‌های base64، parse + dedup
  -> tester.tcp_prefilter()   # فیلتر ارزان TCP قبل از صرف منابع xray
  -> tester.run()             # تست تأخیر واقعی از طریق پراسس(های) xray، ping-sorted alive list
  -> geo.annotate()           # IP -> کد کشور (mmdb محلی یا ip-api.com fallback)
  -> runner._trim()           # نگه داشتن سریع‌ترین N، با حداقل سهم به‌ازای کشور (اختیاری)
  -> rename.build_output()    # نام‌گذاری zone-vpn-xxxxx + ساخت payload نهایی
  -> sign.load_private_key()  # اگر کلید Ed25519 تنظیم شده باشد
  -> gist.publish()           # انتشار JSON / base64 / امضا‌شده در گیست
```

## ماژول‌ها (`zonevpn/`)

| فایل | نقش |
|---|---|
| `__main__.py` | Entry point. حلقهٔ بی‌نهایت با فاصلهٔ زمانی ثابت؛ خطای هر سیکل swallow می‌شود تا پروسه نمیرد. حالت `--once` هم دارد. |
| `config.py` | خواندن `config.json`، پیدا کردن باینری xray، resolve مسیر GeoIP db. |
| `sources.py` | دانلود متن از URLهای منبع (هندل ساب‌های base64-encoded)، parse و dedup لینک‌ها. |
| `links.py` | تبدیل لینک‌های vmess/vless/trojan/ss به آبجکت outbound برای xray؛ `ParsedConfig` دیتاکلاس اصلی؛ rebuild لینک با نام جدید؛ `dedup_key`. |
| `tester.py` | هستهٔ پرفورمنس: batch کردن کانفیگ‌ها در پراسس‌های مشترک xray (هر کانفیگ یک SOCKS inbound) برای تست تأخیر واقعی HTTP با کمترین overhead. شامل TCP pre-filter ارزان. |
| `geo.py` | resolve کشور از روی هاست/IP — اول mmdb محلی، بعد fallback به ip-api.com (batched, rate-limit aware). `flag_emoji()`. |
| `rename.py` | ساخت نام نمایشی نهایی (`zone-vpn-xxxxx` + پرچم) و اسمبل payload خروجی. |
| `gist.py` | ایجاد/آپدیت گیست. `publish()` بین سه حالت انتخاب می‌کند: امضا‌شدهٔ Ed25519 (اولویت اول) > base64 > JSON خوانا. |
| `sign.py` | امضای Ed25519 برای ضد جعل بودن لیست منتشرشده (جزئیات زیر). |
| `runner.py` | اورکستریشن کل سیکل (`run_cycle`) + منطق trim کردن نتایج. |

## فایل‌های ریشه
- `setup_wizard.py` — ویزارد تعاملی که `config.json` را می‌سازد (توکن گیت‌هاب، gist id، منابع، تنظیمات تست) با مجوز `600`.
- `generate_keys.py` — اسکریپت یک‌باره برای تولید جفت‌کلید Ed25519 (امضای گیست).
- `install.sh` — نصب‌کنندهٔ Debian/Ubuntu: نصب وابستگی‌ها، venv، دانلود نسخهٔ پین‌شدهٔ xray-core (v25.12.8، برای پشتیبانی `allowInsecure`)، دانلود GeoIP db، اجرای ویزارد، ساخت/فعال‌سازی سرویس systemd.
- `config.example.json` — تمپلیت تنظیمات.

## ویژگی امنیتی: امضای Ed25519 (اختیاری)
چون گیست عمومی است، خطر MITM/جایگزینی لیست با سرورهای مخرب وجود دارد. راه‌حل: کلید خصوصی روی سرور امضا می‌کند، اپ موبایل کلید عمومی را embedded دارد و امضا را verify می‌کند.

- فعال‌سازی: `python generate_keys.py` → کلید private در `config.json` (`ed25519_private_key` یا `ed25519_private_key_file`) → کلید public در اپ فلاتر (`lib/core/config/secret_endpoint.dart`, `signingPublicKeyB64`, `requireSignature = true`).
- اگر کلیدها ست نشوند، رفتار دقیقاً مثل قبل (بدون امضا) است — کاملاً opt-in.
- فرمت پیاده‌سازی شده در `zonevpn/sign.py` (داک‌استرینگ بالای فایل را ببین برای جزئیات envelope).

## تنظیمات مهم در `config.json`
- `github_token`, `gist_id`, `gist_filename`, `gist_base64`
- `sources` (لیست URL)
- `interval_minutes`, `name_prefix`
- `test.*` (tcp_prefilter, tcp_timeout, tcp_concurrency, max_configs_to_test, max_output, min_per_country, tls_allow_insecure)
- `ed25519_private_key` / `ed25519_private_key_file`

## نکات
- پروژهٔ خواهر/مصرف‌کننده: اپ موبایل فلاتر (در ریپوی جدا) که از همین گیست می‌خواند.
- ریپو: `johnsmith155/zoneserver` (public).
- این فایل را هنگام تغییر معماری/ماژول‌های جدید آپدیت کن؛ جزئیات پیاده‌سازی (خط به خط) را اینجا تکرار نکن — کد منبع حقیقت است.
