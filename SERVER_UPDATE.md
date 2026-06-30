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
#    این مرحله فقط deps را به‌روز می‌کند، dashboard_token می‌سازد، sudoers و دو
#    سرویس systemd را نصب می‌کند. (xray/GeoIP دوباره دانلود می‌شوند — مشکلی نیست.)
sudo bash install.sh
```

آخر کار، نصب‌کننده آدرس داشبورد را با توکن چاپ می‌کند، چیزی شبیه:

```
http://SERVER_IP:8787/?token=XXXXXXXXXXXXXXXXXX
```

این آدرس را در مرورگر باز کن. توکن در `config.json` کلید `dashboard_token` هم هست.

> `install.sh` بی‌خطر است و چند بار می‌شود اجرایش کرد (idempotent): اگر
> `config.json` باشد ویزارد را رد می‌کند و فقط deps/توکن/sudoers/سرویس‌ها را
> به‌روز می‌کند. xray/GeoIP دوباره دانلود می‌شوند که اشکالی ندارد.

---

## دفعات بعد (با یک کلیک)

۱. داشبورد را باز کن: `http://SERVER_IP:8787/?token=...`
۲. دکمهٔ **⬆ Update server** را بزن و تأیید کن.

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
  # بعد در مرورگر: http://localhost:8787/?token=...
  ```
  و در `config.json` مقدار `"dashboard_host": "127.0.0.1"` بگذار.
- اگر `dashboard_token` خالی باشد، داشبورد **بدون احراز هویت** است؛ این حالت را
  فقط پشت فایروال/تونل استفاده کن.
