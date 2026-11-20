Hospital — Offline Site Server (Hospital / Clinic) (Windows 10 / 11)
=====================================================================
ZIP ၂ မျိုး ရှိပါတယ်: Hospital-Setup (ဆေးရုံ) နဲ့ Clinic-Setup (ဆေးခန်း)။
ကိုယ့်နေရာနဲ့ ကိုက်တဲ့ ZIP ကိုပဲ သုံးပါ (မှားထည့်ရင် installer က တားပါတယ်)။

ထည့်နည်း (Main computer မှာ တစ်ခါပဲ)
--------------------------------------
1. ဒီ zip ကို Main computer ပေါ် ကူးပြီး Right-click → "Extract All..." နဲ့ ဖြည်ပါ။
   (zip ထဲကနေ တိုက်ရိုက် မဖွင့်ပါနဲ့)
2. "Install Hospital.bat" ကို double-click လုပ်ပါ။
   - "Windows protected your PC" ပေါ်ရင် → "More info" → "Run anyway"
   - "Do you want to allow this app to make changes" → "Yes"
3. ပေါ်လာတဲ့ window မှာ "Install" ခလုတ်ကို နှိပ်ပါ။ (၁–၃ မိနစ်)
4. ပြီးရင် တခြား computer တွေကနေ ဝင်ရမယ့် လိပ်စာ (ဥပမာ http://192.168.1.10) ကို ပြပါမယ်။

Login: zip နဲ့အတူ ပေးထားတဲ့ logins စာရင်းအတိုင်း ဝင်ပါ။ (Data အလွတ်နဲ့ အသစ်ကနေ စပါတယ်)
ပထမဆုံး ဝင်ပြီးရင် password တွေ ပြောင်းပါ။

Cloud နဲ့ ချိတ်ပုံ (Hybrid)
--------------------------
- ဆေးရုံ / ဆေးခန်း က internet မလိုဘဲ LAN ပေါ်မှာ အလုပ်လုပ်ပါတယ် (Register, Token, TV, Cashier, Lab, Pharmacy ...)။
- Internet ရတာနဲ့ အလိုလို (server ထဲမှာ အမြဲ run နေပါတယ်):
    • ၁၅ စက္ကန့်တစ်ခါ data ကို cloud ဆီ ပို့ပါတယ် → Owner က https://your-cloud-domain.com
      Owner Panel မှာ branch တစ်ခုချင်း / "All Branches" ပေါင်း ကြည့်လို့ရပါတယ်။
    • Item / ဈေးနှုန်း ကို cloud (Warehouse account) မှာ ထည့်/ပြင်ရင် ဒီ server ကို အလိုလို ရောက်လာပါတယ်။
      ဒီ server မှာ item ထည့်/ဈေးပြင်လို့ မရပါ (Stock In / Stock Out / Min stock ကတော့ ရပါတယ်)။
    • Token slip ပေါ်က QR ကို လူနာက ဖုန်း data နဲ့ ဘယ်နေရာကမဆို ဖွင့်လို့ရပါတယ်။
      Internet ပြတ်နေရင် ဖုန်းမှာ "နောက်ဆုံး update" ပြပါမယ် — ဆေးရုံ TV ကတော့ ပုံမှန်အတိုင်း ပြပါတယ်။
- Sync မှတ်တမ်း: C:\Hospital\logs\sync.log

တခြား computer တွေမှာ
---------------------
- ဘာမှ install မလုပ်ရပါ။ Chrome / Edge မှာ အပေါ်က လိပ်စာကို ဖွင့်ရုံပါပဲ။
- Shortcut လိုချင်ရင်: Main computer ရဲ့ C:\Hospital\For-Other-Computers folder ကို USB နဲ့ ကူးပြီး
  "Make Hospital shortcut.bat" ကို double-click လုပ်ပါ။

အရေးကြီးတာတွေ
--------------
- Router မှာ Main computer ကို IP ပုံသေ (DHCP reservation) ပေးထားပါ။ IP ပြောင်းရင် တခြားစက်တွေ ဝင်မရပါ။
- Main computer နဲ့ Router ကို UPS နဲ့ ချိတ်ထားပါ။ (မီးရုတ်တရက်ပျက်ရင် data ပျက်နိုင်ပါတယ်)
- Main computer ကို ပိတ်ထားရင် ဘယ်သူမှ သုံးလို့မရပါ။ Sleep ကို installer က ပိတ်ပေးထားပါတယ်။
- Backup: နေ့တိုင်း 1:00 PM နဲ့ 8:30 PM မှာ C:\Hospital\backups ထဲ အလိုလို သိမ်းပါတယ် (၆၀ ရက်)။
  USB / D: drive ထဲလည်း ထပ်သိမ်းချင်ရင် C:\Hospital\config.env ထဲက
  BACKUP_COPY_TO=D:\HospitalBackup  လို ရေးထားပါ။

Update / ဖြုတ်ခြင်း
-------------------
- Version အသစ်ရရင် zip အသစ်ကို ဖြည်ပြီး "Install Hospital.bat" ကို ပြန်နှိပ်ရုံပါပဲ။ Data မပျက်ပါ။
- ဖြုတ်ချင်ရင် "Uninstall Hospital.bat" (data နဲ့ backup ကိုတော့ ချန်ထားပါမယ်)။

ဖိုင်တည်နေရာ
-------------
C:\Hospital\data\hospital.db     ← database
C:\Hospital\backups\             ← backup များ
C:\Hospital\logs\                ← server.log, backup.log, install.log
C:\Hospital\config.env           ← PORT, SITE, backup copy setting
