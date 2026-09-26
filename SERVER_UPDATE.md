# به‌روزرسانی سرور — راهنمای قدم‌به‌قدم

سروری که الان بالاست، **قبل** از اضافه‌شدن داشبورد و آپدیت خودکار نصب شده. پس
**بار اول باید دستی** آپدیت کنی تا سرویس داشبورد، `update.sh` و قانون sudoers
ساخته شوند. از دفعهٔ بعد، دکمهٔ **Update** داخل داشبورد همین کار را می‌کند.

---

## بار اول (دستی، از طریق SSH)

```bash
# 1) به سرور وصل شو و برو داخل پوشهٔ پروژه
ssh user@SERVER_IP
cd ~/zonevpnserverlogic        # یا هر مسیری که پروژه آنجاست

# 2) آخرین کد را بگیر (هارد، تا با کد سرور یکی شود)
git fetch --all --prune
git reset --hard origin/main

# 3) نصب‌کننده را دوباره اجرا کن. چون config.json از قبل هست، ویزارد رد می‌شود؛
#    این مرحله فقط deps را به‌روز می‌کند، کلیدهای داشبورد را می‌سازد، sudoers و دو
#    سرویس systemd را نصب می‌کند. (xray/GeoIP دوباره دانلود می‌شوند — مشکلی نیست.)
sudo bash install.sh
```

آخر کار، نصب‌کننده آدرس داشبورد را چاپ می‌کند:

```
https://SERVER_IP:8787
```

گواهی HTTPS از Let's Encrypt برای خود IP سرور است (پروفایل shortlived، ۶ روزه) و
مرورگر هشداری نمی‌دهد. certbot از snap نصب است و تایمر `snap.certbot.renew.timer`
روزی دو بار تمدیدش می‌کند؛ داشبورد گواهی تازه را ظرف ۱۰ دقیقه بدون ری‌استارت
برمی‌دارد. مسیرها در `config.json`: `dashboard_cert` و `dashboard_key`. اگر خالی
باشند، داشبورد یک گواهی self-signed برای «localhost» می‌سازد (مرورگر هشدار می‌دهد).
گرفتن دوباره روی سرور تازه (پورت 80 باید آزاد و از بیرون در دسترس باشد):

```bash
snap install --classic certbot
certbot certonly --standalone --ip-address SERVER_IP --preferred-profile shortlived   --non-interactive --agree-tos --register-unsafely-without-email
```
ورود با نام کاربری و رمز است. برای تعیین یا عوض کردنشان روی سرور:

```bash
/root/zoneserver/venv/bin/python -m zonevpn.dashboard set-login
```

فقط هش رمز (scrypt) در `config.json` ذخیره می‌شود و نیازی به ری‌استارت نیست.
قبل از ورود، صفحه فقط یک «Sign in» ساده است و هر آدرس دیگری 404 می‌دهد؛ هیچ
اسم و نشانی از برنامه در پاسخ‌ها نیست که در اسکن‌ها دیده شود.

> `install.sh` بی‌خطر است و چند بار می‌شود اجرایش کرد (idempotent): اگر
> `config.json` باشد ویزارد را رد می‌کند و فقط deps/توکن/sudoers/سرویس‌ها را
> به‌روز می‌کند. xray/GeoIP دوباره دانلود می‌شوند که اشکالی ندارد.

---

## دفعات بعد (با یک کلیک)

۱. داشبورد را باز کن: `https://SERVER_IP:8787` و وارد شو.
۲. دکمهٔ **⬆ Update** را بزن و تأیید کن.

این کار `update.sh` را اجرا می‌کند که:
- سرویس `zonevpn` را **stop** می‌کند،
- کد را `git reset --hard origin/main` می‌کند (هارد آپدیت)،
- اگر `requirements.txt` عوض شده باشد deps را نصب می‌کند،
- هر دو سرویس را **restart** می‌کند و سیکل از همان‌جا ادامه پیدا می‌کند.

`config.json`، پوشهٔ `state/` (blocklist و وضعیت)، باینری xray و دیتابیس GeoIP
چون git-ignored هستند **دست‌نخورده** می‌مانند.

> معادل خط‌فرمان همین دکمه:
> ```bash
> sudo bash ~/zonevpnserverlogic/update.sh
> ```

---

## چک‌کردن وضعیت

```bash
systemctl status zonevpn            # کالکتور
systemctl status zonevpn-dashboard  # داشبورد
journalctl -u zonevpn -f            # لاگ زنده (همان چیزی که داشبورد نشان می‌دهد)
```

## نکات امنیتی داشبورد
- پورت `8787` را فقط برای خودت باز بگذار. امن‌ترین حالت: پورت را روی فایروال
  ببند و با **SSH tunnel** وصل شو:
  ```bash
  ssh -L 8787:localhost:8787 user@SERVER_IP
  # بعد در مرورگر: https://localhost:8787
  ```
  و در `config.json` مقدار `"dashboard_host": "127.0.0.1"` بگذار.
- تا وقتی با `set-login` نام کاربری و رمز تعیین نشده، هیچ‌کس نمی‌تواند وارد شود.
  بعد از ۶ رمز اشتباه از یک آدرس، آن آدرس ۱۵ دقیقه اصلاً بررسی نمی‌شود.
