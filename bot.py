import os
import re
import json
import random
import time
import asyncio
import itertools
import functools
import datetime
import urllib.request

import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp

try:
    import aiohttp
    import aiohttp.web
except ImportError:
    aiohttp = None

# ============================================
# AYARLAR
# ============================================
TOKEN = os.getenv("DISCORD_TOKEN", "BOT_TOKENINI_BURAYA_YAZ")

# Bot'un başlangıç zamanı (/botbilgi için)
BASLANGIC_ZAMANI = time.time()

# Windows'ta ffmpeg.exe PATH'te değilse buraya tam yolunu yazabilirsin,
# örn: r"C:\ffmpeg\bin\ffmpeg.exe"
FFMPEG_YOLU = "ffmpeg"

YTDLP_AYARLARI = {
    "format": "bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch1",
    "source_address": "0.0.0.0",
    # YouTube 2026'da "web"/"android" istemcilerine PO Token şart koşuyor.
    # "android_vr" + "web_embedded" PO Token gerektirmez ve DRM'siz ses formatı
    # verir. "tv" client'ı DRM'li format verdiği için kullanmıyoruz.
    "extractor_args": {
        "youtube": {
            "player_client": ["android_vr", "web_embedded"],
        }
    },
}

# Cookies varsa ekstra güç verir ama client'ı DEĞİŞTİRMEZ (her zaman yukarıdakiler).
COOKIES_DOSYASI = os.getenv("COOKIES_DOSYASI", "cookies.txt")
if not os.path.exists(COOKIES_DOSYASI):
    COOKIES_DOSYASI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")
COOKIES_VAR = os.path.exists(COOKIES_DOSYASI)

if COOKIES_VAR:
    YTDLP_AYARLARI["cookiefile"] = COOKIES_DOSYASI
    print(f"Cookies dosyası kullanılıyor: {COOKIES_DOSYASI}")

FFMPEG_SECENEKLERI = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

# Sunucuya yeni girenlere otomatik verilecek rol. Bu rol tüm normal
# kanallardan gizlenir; sadece bot'un açtığı kişiye özel kanal görünür.
UYE_ROLU_ADI = "Üye"

async def _kanali_uye_rolunden_gizle(kanal: discord.abc.GuildChannel, rol: discord.Role) -> None:
    """Tek bir kanalı 'Üye' rolünden gizler. Ses/sahne kanallarında ekstra olarak
    bağlanma iznini de kapatır ki görse bile sese giremesin."""
    izinler = {"view_channel": False, "reason": "üye rolünden kanalı gizle"}
    if isinstance(kanal, (discord.VoiceChannel, discord.StageChannel)):
        izinler["connect"] = False
    await kanal.set_permissions(rol, **izinler)


async def _uye_rolu_getir_veya_olustur(guild: discord.Guild) -> discord.Role:
    """'Üye' rolünü döndürür; yoksa oluşturur ve mevcut tüm kanallardan gizler."""
    rol = discord.utils.get(guild.roles, name=UYE_ROLU_ADI)
    if rol is not None:
        return rol

    rol = await guild.create_role(
        name=UYE_ROLU_ADI,
        permissions=discord.Permissions.none(),
        reason="otomatik üye rolü - varsayılan kanal erişimini kısıtlamak için",
    )

    # Rol yeni oluşturulduğunda, mevcut tüm kanallardan bu rolü gizle.
    # Böylece "Üye" rolüne sahip biri, bot'un kendisine özel açtığı kanal
    # dışında (o kanalda zaten kişiye özel overwrite var, role ihtiyaç yok)
    # hiçbir kanalı göremez.
    for kanal in guild.channels:
        try:
            await _kanali_uye_rolunden_gizle(kanal, rol)
        except discord.HTTPException:
            pass

    return rol


intents = discord.Intents.default()
intents.voice_states = True
intents.members = True
intents.message_content = True  # isim mesajını okuyabilmek için gerekli

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_guild_channel_create(channel: discord.abc.GuildChannel):
    """Sunucuda yeni bir kanal (metin/ses/kategori) açıldığında, henüz rol almamış
    'Üye' rolündeki kişilerden otomatik olarak gizler. Bu sayede yeni açılan bir
    ses kanalı bile rol verilmeden görünmez/erişilmez olur."""
    try:
        rol = await _uye_rolu_getir_veya_olustur(channel.guild)
        await _kanali_uye_rolunden_gizle(channel, rol)
    except discord.HTTPException as e:
        print(f"Yeni kanal Üye rolünden gizlenemedi ({channel}): {e}")


# ============================================
# VERİ YÖNETİMİ (JSON dosyası)
# ============================================
# NOT: Railway'de dosyalar deploy sırasında sıfırlanır. Kalıcı veri istersen
# ileride bir veritabanı (örn. Postgres) eklemek gerekir. Şimdilik dosyada.
DATA_DOSYASI = "bot_veri.json"


def _varsayilan_veri() -> dict:
    return {
        "xp": {},          # "guild_id:user_id" -> toplam XP
        "uyarilar": {},    # "guild_id:user_id" -> [uyarı kayıtları]
        "log_kanali": {},  # "guild_id" -> kanal_id
        "giris_cikis_kanali": {},  # "guild_id" -> kanal_id
        "koruma": {},      # "guild_id" -> {"link": bool, "kufur": bool, "spam": bool, "yenihesap": bool}
        "cekilisler": {},  # "mesaj_id" -> çekiliş kaydı
        "sabit_kanal": {}, # "guild_id" -> 7/24 sabit ses kanalı id'si
        "sayac": {},      # "guild_id" -> {"uye": {"kanal_id","ad"}, "ses": {...}}
        "duyuru_kapali": {},  # "guild_id:user_id" -> True (genel duyuru almak istemeyenler)
        "begenilenler": {},  # "guild_id:url" -> True (sitede beğenilen şarkılar)
    }


_veri: dict = _varsayilan_veri()


def _veri_yukle() -> None:
    global _veri
    try:
        with open(DATA_DOSYASI, "r", encoding="utf-8") as f:
            yuklenen = json.load(f)
        _veri = _varsayilan_veri()
        _veri.update(yuklenen)
    except (FileNotFoundError, json.JSONDecodeError):
        _veri = _varsayilan_veri()


def _veri_kaydet() -> None:
    try:
        with open(DATA_DOSYASI, "w", encoding="utf-8") as f:
            json.dump(_veri, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"Veri dosyasına yazılamadı: {e}")


# ============================================
# LOG SİSTEMİ
# ============================================
async def _log_gonder(guild: discord.Guild, baslik: str, aciklama: str, renk: discord.Color = discord.Color.blue()):
    """Ayarlanmış log kanalına bir embed gönderir. Kanal ayarlı değilse sessizce döner."""
    kanal_id = _veri["log_kanali"].get(str(guild.id))
    if not kanal_id:
        return
    kanal = guild.get_channel(int(kanal_id))
    if kanal is None or not isinstance(kanal, discord.TextChannel):
        return
    embed = discord.Embed(
        title=baslik,
        description=aciklama,
        color=renk,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    try:
        await kanal.send(embed=embed)
    except discord.HTTPException:
        pass


async def _giris_cikis_gonder(guild: discord.Guild, baslik: str, aciklama: str, renk: discord.Color):
    """Ayarlanmış giriş/çıkış kanalına bir embed gönderir. Kanal ayarlı değilse sessizce döner."""
    kanal_id = _veri["giris_cikis_kanali"].get(str(guild.id))
    if not kanal_id:
        return
    kanal = guild.get_channel(int(kanal_id))
    if kanal is None or not isinstance(kanal, discord.TextChannel):
        return
    embed = discord.Embed(
        title=baslik,
        description=aciklama,
        color=renk,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    try:
        await kanal.send(embed=embed)
    except discord.HTTPException:
        pass


# ============================================
# XP / SEVİYE SİSTEMİ
# ============================================
XP_CD_SANIYE = 60          # aynı kullanıcıya mesaj XP'si için bekleme süresi
XP_MIN, XP_MAX = 5, 15     # mesaj başına verilen XP aralığı
SES_XP_MIKTARI = 2         # sesli kanalda her 60 saniyede verilen XP
SEVIYE_CARPANI = 100       # her seviye için gereken XP miktarı

# "guild_id:user_id" -> son mesaj XP zamanı (sadece bellekte, kalıcılık gerekmez)
xp_son_mesaj: dict[str, float] = {}


def _seviye_hesapla(xp: int) -> tuple[int, int, int]:
    """(mevcut seviye, bu seviyedeki xp, sonraki seviye için gereken xp) döndürür."""
    seviye = 1
    while xp >= SEVIYE_CARPANI * seviye:
        xp -= SEVIYE_CARPANI * seviye
        seviye += 1
    return seviye, xp, SEVIYE_CARPANI * seviye


def _xp_ekle(guild_id: int, user_id: int, miktar: int) -> tuple[int, bool]:
    """XP ekler ve (yeni seviye, seviye atladı mı) döndürür."""
    anahtar = f"{guild_id}:{user_id}"
    onceki_xp = _veri["xp"].get(anahtar, 0)
    onceki_seviye = _seviye_hesapla(onceki_xp)[0]
    yeni_xp = onceki_xp + miktar
    _veri["xp"][anahtar] = yeni_xp
    yeni_seviye = _seviye_hesapla(yeni_xp)[0]
    return yeni_seviye, yeni_seviye > onceki_seviye


async def ses_xp_dongusu():
    """Her 60 saniyede bir, bot dışındaki sesli kanal üyelerine XP verir."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        await asyncio.sleep(60)
        for guild in bot.guilds:
            for kanal in guild.voice_channels:
                if not kanal.permissions_for(guild.me).connect:
                    continue
                for uye in kanal.members:
                    if uye.bot:
                        continue
                    yeni_seviye, atladi = _xp_ekle(guild.id, uye.id, SES_XP_MIKTARI)
                    if atladi:
                        try:
                            await kanal.send(f"🎉 Tebrikler {uye.mention}! **{yeni_seviye}. seviyeye** ulaştın!")
                        except discord.HTTPException:
                            pass
        _veri_kaydet()


async def _sayac_kanali_guncelle(kanal: discord.VoiceChannel, tur: str):
    """Bir sayaç kanalının adını anlık üye/ses sayısına göre günceller."""
    if tur == "uye":
        isim = f"👥 Üye: {kanal.guild.member_count or 0}"
    else:
        isim = f"🎧 Seste: {sum(len(vc.members) for vc in kanal.guild.voice_channels)}"
    if kanal.name != isim:
        try:
            await kanal.edit(name=isim)
        except discord.HTTPException:
            pass


async def sayac_dongusu():
    """Her 60 saniyede bir, ayarlanmış sayaç kanallarını günceller."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        await asyncio.sleep(60)
        for guild in bot.guilds:
            sayaclar = _veri.get("sayac", {}).get(str(guild.id), {})
            for tur, kayit in list(sayaclar.items()):
                kanal = guild.get_channel(int(kayit["kanal_id"]))
                if kanal is None or not isinstance(kanal, discord.VoiceChannel):
                    continue
                await _sayac_kanali_guncelle(kanal, tur)
INVITE_DESENI = re.compile(r"discord(?:\.gg|\.com/invite|app\.com/invite)/[A-Za-z0-9_-]+")

KUFUR_KELIMELERI = [
    "amk", "amq", "aq", "oç", "piç", "gavat", "ibne", "kaşar",
    "yarrak", "orospu", "sikik", "ananı", "dangalak", "mal", "aptal",
]

KORUMA_OZELLIKLERI = {
    "link": "Davet/reklam linkleri",
    "kufur": "Küfür filtresi",
    "spam": "Spam koruması",
    "yenihesap": "Yeni hesap koruması",
}

YENI_HESAP_GUN = 14     # bu günden küçük hesaplar atılır (açıksa)
SPAM_ESIK_SANIYE = 5    # bu süre içinde
SPAM_ESIK_ADET = 4      # bu kadar çok mesaj = spam

# "guild_id:user_id" -> son mesaj zamanları (bellek içi, geçici)
spam_zamanlar: dict[str, list[float]] = {}


def _kufur_mu(metin: str) -> bool:
    return any(k in metin for k in KUFUR_KELIMELERI)


async def _koruma_kontrol(mesaj: discord.Message) -> bool:
    """Mesajı koruma kurallarına göre kontrol eder. İhlal varsa siler ve uyarır;
    mesaj silindiyse True döndürür."""
    guild = mesaj.guild
    uye = mesaj.author

    # Yöneticiler ve mesaj yönetebilenler muaf
    if uye.guild_permissions.administrator or uye.guild_permissions.manage_messages:
        return False

    ayarlar = _veri["koruma"].get(str(guild.id), {})
    icerik = mesaj.content.lower()
    ihlal = None

    if ayarlar.get("link") and INVITE_DESENI.search(icerik):
        ihlal = "reklam/davet linki"
    elif ayarlar.get("kufur") and _kufur_mu(icerik):
        ihlal = "küfür"
    elif ayarlar.get("spam"):
        anahtar = f"{guild.id}:{uye.id}"
        simdi = time.time()
        liste = [t for t in spam_zamanlar.get(anahtar, []) if simdi - t <= SPAM_ESIK_SANIYE]
        liste.append(simdi)
        spam_zamanlar[anahtar] = liste
        if len(liste) > SPAM_ESIK_ADET:
            ihlal = "spam"

    if ihlal is None:
        return False

    try:
        await mesaj.delete()
    except discord.HTTPException:
        return True

    try:
        await uye.send(f"⚠️ **{guild.name}** sunucusunda mesajın **{ihlal}** içerdiği için silindi.")
    except discord.HTTPException:
        pass

    await _log_gonder(
        guild,
        "🛡️ Koruma",
        f"{uye.mention} bir mesajı **{ihlal}** nedeniyle silindi.",
        discord.Color.red(),
    )
    return True


# ============================================
# TICKET SİSTEMİ
# ============================================
TICKET_KATEGORI_ADI = "Tickets"
TICKET_KANAL_ON_EK = "ticket-"


class TicketCloseView(discord.ui.View):
    """Ticket kanalının içine gönderilen mesajdaki 'Close Ticket' butonu."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close Ticket", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="ticket_close_button")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        kanal = interaction.channel
        if not isinstance(kanal, discord.TextChannel) or not kanal.name.startswith(TICKET_KANAL_ON_EK):
            await interaction.response.send_message("Bu buton sadece ticket kanallarında çalışır.", ephemeral=True)
            return

        await interaction.response.send_message("🔒 Ticket kapatılıyor, kanal 5 saniye içinde silinecek...")
        await asyncio.sleep(5)
        try:
            await kanal.delete(reason=f"{interaction.user} tarafından ticket kapatıldı")
        except discord.HTTPException as e:
            try:
                await kanal.send(f"⚠️ Kanal silinemedi: {e}")
            except discord.HTTPException:
                pass


class TicketPanelView(discord.ui.View):
    """Panel mesajındaki 'Create ticket' butonu."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Revolution", emoji="📩", style=discord.ButtonStyle.secondary, custom_id="ticket_create_button")
    async def create_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Bu buton sadece sunucuda çalışır.", ephemeral=True)
            return

        kanal_adi = f"{TICKET_KANAL_ON_EK}{interaction.user.id}"
        mevcut = discord.utils.get(guild.text_channels, name=kanal_adi)
        if mevcut is not None:
            await interaction.response.send_message(
                f"Zaten açık bir ticket'ın var: {mevcut.mention}", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        kategori = discord.utils.get(guild.categories, name=TICKET_KATEGORI_ADI)
        if kategori is None:
            try:
                kategori = await guild.create_category(TICKET_KATEGORI_ADI, reason="ticket sistemi kategorisi")
            except discord.HTTPException as e:
                await interaction.followup.send(f"⚠️ Ticket kategorisi oluşturulamadı: {e}", ephemeral=True)
                return

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, manage_channels=True, read_message_history=True
            ),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True, attach_files=True
            ),
        }
        # Yönetici (administrator) yetkisine sahip tüm roller de görebilsin
        for rol in guild.roles:
            if rol.permissions.administrator:
                overwrites[rol] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True
                )

        try:
            ticket_kanal = await guild.create_text_channel(
                kanal_adi,
                category=kategori,
                overwrites=overwrites,
                reason=f"{interaction.user} ticket açtı",
            )
        except discord.HTTPException as e:
            await interaction.followup.send(f"⚠️ Ticket kanalı oluşturulamadı: {e}", ephemeral=True)
            return

        embed = discord.Embed(
            title="🎫 Destek Talebi",
            description=(
                f"Merhaba {interaction.user.mention}! Sorununu buraya olabildiğince detaylı yaz, "
                f"yetkili ekibimiz en kısa sürede sana dönecek.\n\nİşin bittiğinde aşağıdaki butonla "
                f"ticket'ı kapatabilirsin."
            ),
            color=discord.Color.blurple(),
        )
        await ticket_kanal.send(content=interaction.user.mention, embed=embed, view=TicketCloseView())
        await interaction.followup.send(f"✅ Ticket'ın oluşturuldu: {ticket_kanal.mention}", ephemeral=True)


@bot.tree.command(name="ticketpanel", description="Bu kanala destek ticket paneli mesajını gönderir.")
@app_commands.checks.has_permissions(manage_guild=True)
async def ticketpanel(interaction: discord.Interaction):
    if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("Bu komut sadece bir metin kanalında kullanılabilir.", ephemeral=True)
        return

    embed = discord.Embed(
        description=(
            "📩 **Yardım/Partnerlik/Ally/Yetkili Alim için Ticket Açabilirsin**\n\n"
            "If you have any **questions**, **issues**, or need **support**, our team is here to help you.\n\n"
            "📩 **Ticket Açmak** için alltaki butona tıkla.\n"
            "Sorununu **Direk Belirt** işimizin hızlanması için.\n"
        ),
        color=discord.Color.blurple(),
    )
    await interaction.channel.send(embed=embed, view=TicketPanelView())
    await interaction.response.send_message("✅ Ticket paneli gönderildi.", ephemeral=True)


# Her guild + user için çalışan "gezdirme" görevlerini tutuyoruz
# key: (guild_id, user_id) -> asyncio.Task
gezdirme_gorevleri: dict[tuple[int, int], asyncio.Task] = {}

# Mute/deafen "kilidi" - kullanıcı kendi açsa bile bot anında tekrar kapatır
# key: (guild_id, user_id) -> {"mute": bool, "deafen": bool}
kilitli_durumlar: dict[tuple[int, int], dict[str, bool]] = {}

# İsim kilidi - kullanıcı ismini değiştirse bile bot geri düzeltir
# key: (guild_id, user_id) -> kilitli isim
isim_kilitleri: dict[tuple[int, int], str] = {}

# Sesten atma kilidi - kullanıcı sese her girdiğinde bot onu hemen atar
# key: (guild_id, user_id) -> True
atma_kilitleri: dict[tuple[int, int], bool] = {}

# Godmode - kullanıcı başka bir kanala çekilirse bot onu "ev" kanalına geri döndürür
# key: (guild_id, user_id) -> ev kanalının id'si
godmode_kilitleri: dict[tuple[int, int], int] = {}


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    key = (after.guild.id, after.id)
    kilitli_isim = isim_kilitleri.get(key)
    if kilitli_isim is not None and after.nick != kilitli_isim:
        try:
            await after.edit(nick=kilitli_isim, reason="isim kilidi")
        except discord.HTTPException:
            pass


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    # Bot'un kendisi sesten çıkarıldıysa (biri attıysa, bağlantı koptuysa vs.)
    # kısa bir bekleme sonrası otomatik olarak sabit 7/24 ses kanalına geri döner.
    if member.id == bot.user.id and before.channel is not None and after.channel is None:
        async def _otomatik_geri_baglan(guild: discord.Guild):
            await asyncio.sleep(2)
            try:
                await _sabit_ses_kanaline_baglan(guild, zorla_tasi=True)
            except Exception as e:
                print(f"7/24 ses kanalına otomatik geri bağlanma başarısız ({guild.name}): {e}")

        bot.loop.create_task(_otomatik_geri_baglan(member.guild))

    key = (member.guild.id, member.id)

    # Godmode: biri kullanıcıyı başka kanala çekerse (gezdir, elle taşıma vs.)
    # bot onu anında "ev" kanalına geri döndürür
    ev_kanal_id = godmode_kilitleri.get(key)
    if ev_kanal_id is not None:
        if after.channel is not None and after.channel.id != ev_kanal_id:
            ev_kanal = member.guild.get_channel(ev_kanal_id)
            if ev_kanal is not None:
                try:
                    await member.move_to(ev_kanal, reason="godmode")
                except discord.HTTPException:
                    pass
                return
            else:
                # ev kanal silinmiş, kilidi temizle
                godmode_kilitleri.pop(key, None)

        # Godmode: biri onu susturur/sağırlaştırırsa bot anında geri açar
        if after.channel is not None and (after.mute or after.deaf):
            duzeltmeler = {}
            if after.mute:
                duzeltmeler["mute"] = False
            if after.deaf:
                duzeltmeler["deafen"] = False
            try:
                await member.edit(reason="godmode - mute bağışıklığı", **duzeltmeler)
            except discord.HTTPException:
                pass
            return

    # Sesten atma kilidi: sese her girdiğinde hemen at
    if atma_kilitleri.get(key) and after.channel is not None:
        try:
            await member.move_to(None, reason="atkovala şakası")
        except discord.HTTPException:
            pass
        return  # zaten atıldığı için mute/deafen kontrolüne gerek yok

    kilit = kilitli_durumlar.get(key)
    if kilit is None:
        return
    # Kullanıcı sesten tamamen çıktıysa kilidi temizle (uygulayacak kanal yok)
    if after.channel is None:
        return
    duzeltmeler = {}
    if kilit.get("mute") and not after.mute:
        duzeltmeler["mute"] = True
    if kilit.get("deafen") and not after.deaf:
        duzeltmeler["deafen"] = True
    if duzeltmeler:
        try:
            await member.edit(reason="kilitli mute/deafen", **duzeltmeler)
        except discord.HTTPException:
            pass


@bot.event
async def on_message(mesaj: discord.Message):
    """Mesaj XP'si + anti-spam/anti-link koruması."""
    if mesaj.author.bot or mesaj.guild is None:
        return

    # Önce koruma kontrolü: ihlal varsa mesaj silinir, XP verilmez
    if await _koruma_kontrol(mesaj):
        return

    # --- XP: mesaj başına, cooldown'lu ---
    anahtar = f"{mesaj.guild.id}:{mesaj.author.id}"
    son = xp_son_mesaj.get(anahtar, 0)
    simdi = time.time()
    if simdi - son >= XP_CD_SANIYE:
        xp_son_mesaj[anahtar] = simdi
        miktar = random.randint(XP_MIN, XP_MAX)
        yeni_seviye, atladi = _xp_ekle(mesaj.guild.id, mesaj.author.id, miktar)
        _veri_kaydet()
        if atladi:
            try:
                await mesaj.channel.send(f"🎉 Tebrikler {mesaj.author.mention}! **{yeni_seviye}. seviyeye** ulaştın!")
            except discord.HTTPException:
                pass

    # Prefix ("!") komutları işle (şu an yok ama güvenlik için)
    await bot.process_commands(mesaj)


@bot.event
async def on_message_delete(mesaj: discord.Message):
    if mesaj.guild is None or mesaj.author.bot:
        return
    icerik = mesaj.content[:500] if mesaj.content else "(içerik yok - ek/medya olabilir)"
    await _log_gonder(
        mesaj.guild,
        "🗑️ Mesaj Silindi",
        f"{mesaj.author.mention} **#{mesaj.channel}** kanalında: {icerik}",
        discord.Color.red(),
    )


@bot.event
async def on_member_remove(member: discord.Member):
    await _log_gonder(
        member.guild,
        "📤 Üye Ayrıldı",
        f"{member.mention} ({member.name}) sunucudan ayrıldı.",
        discord.Color.orange(),
    )
    await _giris_cikis_gonder(
        member.guild,
        "📤 Üye Ayrıldı",
        f"{member.mention} ({member.name}) sunucudan ayrıldı.",
        discord.Color.orange(),
    )


@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.User):
    await _log_gonder(
        guild,
        "🔨 Üye Yasaklandı",
        f"{user.mention} ({user.name}) sunucudan yasaklandı.",
        discord.Color.red(),
    )


@bot.event
async def on_member_unban(guild: discord.Guild, user: discord.User):
    await _log_gonder(
        guild,
        "♻️ Yasağı Kaldırıldı",
        f"{user.mention} ({user.name}) yasağı kaldırıldı.",
        discord.Color.green(),
    )


@bot.event
async def on_ready():
    # Ticket butonları bot yeniden başlasa bile çalışmaya devam etsin diye
    # persistent view'ları burada (her başlangıçta) kaydediyoruz.
    bot.add_view(TicketPanelView())
    bot.add_view(TicketCloseView())

    # Rich presence: profil kartında tıklanabilir "Müzik Paneli" bağlantısı.
    try:
        aktivite = discord.Activity(
            name="Müzik Paneli",
            type=discord.ActivityType.listening,
            state="🎧 Müzik Paneli'ni Aç",
            state_url=PUBLIK_URL,
            details="Siteden şarkı çal",
            details_url=PUBLIK_URL,
        )
        await bot.change_presence(activity=aktivite)
    except Exception as e:
        print(f"Presence ayarlanamadı: {e}")

    # Web panelini bot'un event loop'unda başlat (bir kez).
    if not getattr(bot, "_web_basladi", False):
        bot._web_basladi = True
        try:
            bot.loop.create_task(_web_baslat())
        except Exception as e:
            print(f"Web paneli başlatılamadı: {e}")

    # Devam eden çekilişlerin butonlarını ve süre sayaçlarını geri yükle
    for mid_str, kayit in _veri["cekilisler"].items():
        if kayit.get("bitti"):
            continue
        try:
            bot.add_view(CekilisView(int(mid_str)))
        except Exception as e:
            print(f"Çekiliş view'ı eklenemedi ({mid_str}): {e}")
        cekilis_gorevleri[int(mid_str)] = bot.loop.create_task(_cekilis_sayaci(int(mid_str)))

    # Ses XP + sayaç döngülerini bir kez başlat (reconnect'lerde tekrar başlatma)
    if not getattr(bot, "_ses_xp_basladi", False):
        bot._ses_xp_basladi = True
        bot.loop.create_task(ses_xp_dongusu())
        bot.loop.create_task(sayac_dongusu())

    try:
        synced = await bot.tree.sync()
        print(f"{len(synced)} slash komut global olarak senkronize edildi (yayılması ~1 saate kadar sürebilir).")
    except Exception as e:
        print(f"Global senkron hatası: {e}")

    # Global senkron Discord tarafında geç yayılabildiği için, komutların her
    # sunucuda ANINDA görünmesi için ayrıca guild bazlı da senkronluyoruz.
    for guild in bot.guilds:
        try:
            guild_synced = await bot.tree.sync(guild=guild)
            print(f"'{guild.name}' sunucusunda {len(guild_synced)} komut anında senkronize edildi.")
        except Exception as e:
            print(f"'{guild.name}' guild senkron hatası: {e}")

        try:
            await _uye_rolu_getir_veya_olustur(guild)
        except discord.HTTPException as e:
            print(f"'{UYE_ROLU_ADI}' rolü hazırlanamadı ({guild.name}): {e}")

        try:
            await _sabit_ses_kanaline_baglan(guild)
        except Exception as e:
            print(f"7/24 ses kanalına bağlanılamadı ({guild.name}): {e}")

    print(f"{bot.user} olarak giriş yapıldı.")


@bot.event
async def on_member_join(member: discord.Member):
    guild = member.guild

    # --- 0a) Yeni hesap koruması (opsiyonel): hesabı çok yeni olanları at ---
    ayarlar = _veri["koruma"].get(str(guild.id), {})
    if ayarlar.get("yenihesap"):
        hesap_yasi = datetime.datetime.now(datetime.timezone.utc) - member.created_at
        if hesap_yasi.days < YENI_HESAP_GUN:
            try:
                await member.kick(reason=f"Yeni hesap koruması (hesap {hesap_yasi.days} günlük)")
            except discord.HTTPException as e:
                print(f"Yeni hesap kick edilemedi ({member}): {e}")
            else:
                await _log_gonder(
                    guild,
                    "🛡️ Yeni Hesap Kick",
                    f"{member.mention} hesabı **{hesap_yasi.days} günlük** olduğu için atıldı.",
                    discord.Color.red(),
                )
            return

    await _log_gonder(
        guild,
        "📥 Üye Katıldı",
        f"{member.mention} ({member.name}) sunucuya katıldı.",
        discord.Color.green(),
    )
    await _giris_cikis_gonder(
        guild,
        "📥 Üye Katıldı",
        f"{member.mention} ({member.name}) sunucuya katıldı.",
        discord.Color.green(),
    )

    # --- 0) 'Üye' rolünü ver (kilitle/koruma sistemi bu role bağlı) ---
    try:
        uye_rolu = await _uye_rolu_getir_veya_olustur(guild)
        await member.add_roles(uye_rolu, reason="sunucuya giriş - otomatik üye rolü")
    except discord.HTTPException as e:
        print(f"Üye rolü verilemedi ({member}): {e}")


@bot.tree.command(
    name="kilitle",
    description="Tüm kanalları 'Üye' rolünden gizler (geriye dönük, yeni ses kanalları dahil).",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def kilitle(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    try:
        rol = await _uye_rolu_getir_veya_olustur(guild)
    except discord.HTTPException as e:
        await interaction.followup.send(f"'{UYE_ROLU_ADI}' rolü hazırlanamadı: {e}", ephemeral=True)
        return

    basarili = 0
    basarisiz = 0
    for kanal in guild.channels:
        try:
            await _kanali_uye_rolunden_gizle(kanal, rol)
            basarili += 1
        except discord.HTTPException:
            basarisiz += 1

    ozet = f"🔒 **{basarili}** kanal '{UYE_ROLU_ADI}' rolünden gizlendi/kilitlendi."
    if basarisiz:
        ozet += f" ({basarisiz} kanalda izin ayarlanamadı, bot'un yetkisini kontrol et.)"
    await interaction.followup.send(ozet, ephemeral=True)


async def gezdir_loop(member: discord.Member, kanallar: list[discord.VoiceChannel]):
    """Kullanıcıyı sırayla ses kanalları arasında gezdirir (~1 sn arayla)."""
    kanal_dongusu = itertools.cycle(kanallar)
    try:
        while True:
            hedef = next(kanal_dongusu)
            # Kullanıcı hala sesteyse ve zaten o kanalda değilse taşı
            if member.voice is not None:
                if member.voice.channel is None or member.voice.channel.id != hedef.id:
                    try:
                        await member.move_to(hedef, reason="gezdir şakası")
                    except discord.HTTPException:
                        # rate limit veya izin hatası olursa bir sonraki turda tekrar dener
                        pass
            else:
                # Kullanıcı sesten ayrıldıysa görevi durdur
                break
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        # /stop ile iptal edildi, normal çıkış
        raise


@bot.tree.command(name="gezdir", description="Bir kullanıcıyı ses kanalları arasında gezdirir (şaka).")
@app_commands.describe(user="Gezdirilecek kullanıcı")
@app_commands.checks.has_permissions(move_members=True)
async def gezdir(interaction: discord.Interaction, user: discord.Member):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return

    if user.voice is None or user.voice.channel is None:
        await interaction.response.send_message(
            f"{user.mention} şu anda bir ses kanalında değil.", ephemeral=True
        )
        return

    # Botun move_members izni var mı kontrol et
    if not interaction.guild.me.guild_permissions.move_members:
        await interaction.response.send_message(
            "Botun 'Üyeleri Taşı' (Move Members) iznine ihtiyacım var.", ephemeral=True
        )
        return

    komutu_kullananin_kanali = None
    if isinstance(interaction.user, discord.Member) and interaction.user.voice is not None:
        komutu_kullananin_kanali = interaction.user.voice.channel

    kanallar = [
        vc for vc in interaction.guild.voice_channels
        if vc.permissions_for(interaction.guild.me).connect
        and (komutu_kullananin_kanali is None or vc.id != komutu_kullananin_kanali.id)
    ]

    if len(kanallar) < 2:
        await interaction.response.send_message(
            "Gezdirmek için (senin bulunduğun kanal hariç) en az 2 tane erişilebilir ses kanalı olmalı.",
            ephemeral=True,
        )
        return

    key = (interaction.guild.id, user.id)

    # Zaten çalışan bir görev varsa önce onu iptal et
    if key in gezdirme_gorevleri:
        gezdirme_gorevleri[key].cancel()

    task = bot.loop.create_task(gezdir_loop(user, kanallar))
    gezdirme_gorevleri[key] = task

    await interaction.response.send_message(
        f"{user.mention} artık ses kanalları arasında gezdiriliyor. Durdurmak için `/stop` kullan."
    )


@bot.tree.command(name="stop", description="Bir kullanıcıyı gezdirmeyi durdurur.")
@app_commands.describe(user="Gezdirmesi durdurulacak kullanıcı")
@app_commands.checks.has_permissions(move_members=True)
async def stop(interaction: discord.Interaction, user: discord.Member):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return

    key = (interaction.guild.id, user.id)
    task = gezdirme_gorevleri.pop(key, None)

    if task is None:
        await interaction.response.send_message(
            f"{user.mention} için çalışan bir gezdirme yok.", ephemeral=True
        )
        return

    task.cancel()
    await interaction.response.send_message(f"{user.mention} için gezdirme durduruldu.")


async def _mute_uygula(interaction: discord.Interaction, user: discord.Member, mute: bool, deafen: bool):
    """Ortak yardımcı: kilidi ayarlar ve mevcut ses durumuna hemen uygular."""
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return

    if user.voice is None or user.voice.channel is None:
        await interaction.response.send_message(
            f"{user.mention} bir ses kanalında değilken susturulamaz/sağırlaştırılamaz.", ephemeral=True
        )
        return

    perms = interaction.guild.me.guild_permissions
    if mute and not perms.mute_members:
        await interaction.response.send_message("Botun 'Üyeleri Sustur' iznine ihtiyacım var.", ephemeral=True)
        return
    if deafen and not perms.deafen_members:
        await interaction.response.send_message("Botun 'Üyeleri Sağırlaştır' iznine ihtiyacım var.", ephemeral=True)
        return

    key = (interaction.guild.id, user.id)
    kilit = kilitli_durumlar.setdefault(key, {"mute": False, "deafen": False})
    if mute:
        kilit["mute"] = True
    if deafen:
        kilit["deafen"] = True

    duzenle = {}
    if mute:
        duzenle["mute"] = True
    if deafen:
        duzenle["deafen"] = True

    try:
        await user.edit(reason="mute şakası", **duzenle)
    except discord.HTTPException as e:
        await interaction.response.send_message(f"Uygulanamadı: {e}", ephemeral=True)
        return

    parcalar = []
    if mute:
        parcalar.append("susturuldu")
    if deafen:
        parcalar.append("sağırlaştırıldı")
    await interaction.response.send_message(
        f"{user.mention} {' ve '.join(parcalar)} (kilitli, kendi açsa da tekrar kapanacak). "
        f"Durdurmak için `/mutedurdur` kullan."
    )


@bot.tree.command(name="mute", description="Kullanıcıyı susturur ve kilitler (kendi açarsa tekrar susturur).")
@app_commands.describe(user="Susturulacak kullanıcı")
@app_commands.checks.has_permissions(mute_members=True)
async def mute(interaction: discord.Interaction, user: discord.Member):
    await _mute_uygula(interaction, user, mute=True, deafen=False)


@bot.tree.command(name="mutesg", description="Kullanıcıyı sağırlaştırır ve kilitler (kendi açarsa tekrar sağırlaştırır).")
@app_commands.describe(user="Sağırlaştırılacak kullanıcı")
@app_commands.checks.has_permissions(deafen_members=True)
async def mutesg(interaction: discord.Interaction, user: discord.Member):
    await _mute_uygula(interaction, user, mute=False, deafen=True)


@bot.tree.command(name="muteall", description="Kullanıcıyı hem susturur hem sağırlaştırır, ikisini de kilitler.")
@app_commands.describe(user="Susturulup sağırlaştırılacak kullanıcı")
@app_commands.checks.has_permissions(mute_members=True, deafen_members=True)
async def muteall(interaction: discord.Interaction, user: discord.Member):
    await _mute_uygula(interaction, user, mute=True, deafen=True)


@bot.tree.command(name="mutedurdur", description="Kullanıcı için mute/deafen kilidini kaldırır ve geri açar.")
@app_commands.describe(user="Kilidi kaldırılacak kullanıcı")
@app_commands.checks.has_permissions(mute_members=True)
async def mutedurdur(interaction: discord.Interaction, user: discord.Member):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return

    key = (interaction.guild.id, user.id)
    kilit = kilitli_durumlar.pop(key, None)

    if kilit is None:
        await interaction.response.send_message(f"{user.mention} için aktif bir kilit yok.", ephemeral=True)
        return

    if user.voice is not None and user.voice.channel is not None:
        try:
            await user.edit(mute=False, deafen=False, reason="mute kilidi kaldırıldı")
        except discord.HTTPException:
            pass

    await interaction.response.send_message(f"{user.mention} için mute/sağırlaştırma kilidi kaldırıldı.")


@bot.tree.command(name="ceza", description="Kullanıcıya DM'den 15 kere 'adk atma' yazar (şaka).")
@app_commands.describe(user="DM atılacak kullanıcı")
@app_commands.checks.has_permissions(moderate_members=True)
async def ceza(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.send_message(f"{user.mention} kişisine mesaj gönderiliyor...", ephemeral=True)
    gonderilen = 0
    mesaj = f"{user.mention} adk atma"
    try:
        for _ in range(15):
            await user.send(mesaj)
            gonderilen += 1
            await asyncio.sleep(1)
    except discord.Forbidden:
        await interaction.followup.send(
            f"{user.mention} kişisinin DM'leri kapalı, {gonderilen} mesaj gönderildikten sonra durduruldu.",
            ephemeral=True,
        )
        return
    except discord.HTTPException:
        await interaction.followup.send(
            f"Bir hata oldu, {gonderilen} mesaj gönderildi.", ephemeral=True
        )
        return

    await interaction.followup.send(f"{user.mention} kişisine 15 mesaj gönderildi.", ephemeral=True)


@bot.tree.command(name="isimkilit", description="Kullanıcının takma adını verdiğin isme kilitler (kendi değiştirse geri döner).")
@app_commands.describe(user="İsmi kilitlenecek kullanıcı", isim="Kilitlenecek takma isim")
@app_commands.checks.has_permissions(manage_nicknames=True)
async def isimkilit(interaction: discord.Interaction, user: discord.Member, isim: str):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return

    if not interaction.guild.me.guild_permissions.manage_nicknames:
        await interaction.response.send_message("Botun 'Takma Adları Yönet' iznine ihtiyacım var.", ephemeral=True)
        return

    if user.top_role >= interaction.guild.me.top_role:
        await interaction.response.send_message(
            f"{user.mention} kullanıcısının rolü benimkinden yüksek/eşit, ismini değiştiremem.", ephemeral=True
        )
        return

    key = (interaction.guild.id, user.id)
    isim_kilitleri[key] = isim

    try:
        await user.edit(nick=isim, reason="isimkilit şakası")
    except discord.HTTPException as e:
        await interaction.response.send_message(f"Uygulanamadı: {e}", ephemeral=True)
        return

    await interaction.response.send_message(
        f"{user.mention} kullanıcısının ismi '{isim}' olarak kilitlendi. Durdurmak için `/isimkilitdurdur` kullan."
    )


@bot.tree.command(name="isimkilitdurdur", description="Kullanıcının isim kilidini kaldırır.")
@app_commands.describe(user="Kilidi kaldırılacak kullanıcı")
@app_commands.checks.has_permissions(manage_nicknames=True)
async def isimkilitdurdur(interaction: discord.Interaction, user: discord.Member):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return

    key = (interaction.guild.id, user.id)
    if isim_kilitleri.pop(key, None) is None:
        await interaction.response.send_message(f"{user.mention} için aktif bir isim kilidi yok.", ephemeral=True)
        return

    await interaction.response.send_message(f"{user.mention} için isim kilidi kaldırıldı.")


@bot.tree.command(name="atkovala", description="Kullanıcı ses kanalına her girdiğinde onu hemen atar.")
@app_commands.describe(user="Sesten atılacak kullanıcı")
@app_commands.checks.has_permissions(move_members=True)
async def atkovala(interaction: discord.Interaction, user: discord.Member):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return

    if not interaction.guild.me.guild_permissions.move_members:
        await interaction.response.send_message("Botun 'Üyeleri Taşı' iznine ihtiyacım var.", ephemeral=True)
        return

    key = (interaction.guild.id, user.id)
    atma_kilitleri[key] = True

    # Şu an sesteyse hemen at
    if user.voice is not None and user.voice.channel is not None:
        try:
            await user.move_to(None, reason="atkovala şakası")
        except discord.HTTPException:
            pass

    await interaction.response.send_message(
        f"{user.mention} artık ses kanalına her girdiğinde hemen atılacak. "
        f"Durdurmak için `/atkovaladurdur` kullan."
    )


@bot.tree.command(name="atkovaladurdur", description="Kullanıcı için sesten atma kilidini kaldırır.")
@app_commands.describe(user="Kilidi kaldırılacak kullanıcı")
@app_commands.checks.has_permissions(move_members=True)
async def atkovaladurdur(interaction: discord.Interaction, user: discord.Member):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return

    key = (interaction.guild.id, user.id)
    if atma_kilitleri.pop(key, None) is None:
        await interaction.response.send_message(f"{user.mention} için aktif bir atma kilidi yok.", ephemeral=True)
        return

    await interaction.response.send_message(f"{user.mention} için sesten atma kilidi kaldırıldı.")


@bot.tree.command(name="godmode", description="Kullanıcıyı 'dokunulmaz' yapar: kanaldan çekilemez, susturulamaz/sağırlaştırılamaz.")
@app_commands.describe(user="Dokunulmaz yapılacak kullanıcı")
@app_commands.checks.has_permissions(move_members=True, mute_members=True, deafen_members=True)
async def godmode(interaction: discord.Interaction, user: discord.Member):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return

    if user.voice is None or user.voice.channel is None:
        await interaction.response.send_message(
            f"{user.mention} bir ses kanalında değilken godmode açılamaz.", ephemeral=True
        )
        return

    perms = interaction.guild.me.guild_permissions
    if not (perms.move_members and perms.mute_members and perms.deafen_members):
        await interaction.response.send_message(
            "Botun 'Üyeleri Taşı', 'Üyeleri Sustur' ve 'Üyeleri Sağırlaştır' izinlerine ihtiyacım var.",
            ephemeral=True,
        )
        return

    key = (interaction.guild.id, user.id)
    godmode_kilitleri[key] = user.voice.channel.id

    # O an susturulmuş/sağırlaştırılmışsa hemen aç
    if user.voice.mute or user.voice.deaf:
        try:
            await user.edit(mute=False, deafen=False, reason="godmode açıldı")
        except discord.HTTPException:
            pass

    await interaction.response.send_message(
        f"{user.mention} artık dokunulmaz — biri onu başka kanala çekerse **{user.voice.channel.name}** "
        f"kanalına geri dönecek, susturmaya/sağırlaştırmaya çalışılırsa anında geri açılacak. "
        f"Durdurmak için `/godmodedurdur` kullan."
    )


@bot.tree.command(name="godmodedurdur", description="Kullanıcının godmode'unu kaldırır.")
@app_commands.describe(user="Godmode'u kaldırılacak kullanıcı")
@app_commands.checks.has_permissions(move_members=True)
async def godmodedurdur(interaction: discord.Interaction, user: discord.Member):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return

    key = (interaction.guild.id, user.id)
    if godmode_kilitleri.pop(key, None) is None:
        await interaction.response.send_message(f"{user.mention} için aktif bir godmode yok.", ephemeral=True)
        return

    await interaction.response.send_message(f"{user.mention} için godmode kaldırıldı.")


# ============================================
# MÜZİK SIRASI (Spotify tarzı kuyruk sistemi)
# ============================================

class SarkiKaydi:
    """Kuyruktaki bir şarkının kaydı. yt-dlp'nin verdiği stream linkleri zamanla süresi
    dolduğu için kuyrukta sadece arama/sayfa linkini tutuyoruz; gerçek stream URL'i
    şarkı çalınmaya başlarken tazeden çekiliyor."""

    def __init__(self, sorgu: str, baslik: str, isteyen: discord.Member, kanal: discord.abc.Messageable,
                 thumbnail: str | None = None, sure: str | None = None, sure_sn: float | None = None):
        self.sorgu = sorgu
        self.baslik = baslik
        self.isteyen = isteyen
        self.kanal = kanal
        self.thumbnail = thumbnail
        self.sure = sure
        self.sure_sn = sure_sn


class MuzikSirasi:
    def __init__(self):
        self.kuyruk: list[SarkiKaydi] = []
        self.simdi_calan: SarkiKaydi | None = None
        self.baslama_zamani: float | None = None      # şarkının başladığı zaman (epoch)
        self.duraklatma_an: float | None = None       # son duraklatma anı (epoch)
        self.toplam_duraklatma: float = 0.0           # toplam duraklatılan süre (sn)
        self.dongu: int = 0                           # 0=kapalı, 1=şarkı, 2=kuyruk
        self.dongu_cevir: list[SarkiKaydi] = []       # kuyruk döngüsü için anlık görüntü
        self.dongu_atlama: bool = False               # skip'te şarkı döngüsünü bastırır
        self.autoplay: bool = False                   # kuyruk bitince benzer şarkı çal
        self.gecmis: list[SarkiKaydi] = []            # son çalınanlar (max 25)


muzik_siralari: dict[int, MuzikSirasi] = {}  # guild_id -> MuzikSirasi


def _sira_al(guild_id: int) -> MuzikSirasi:
    sira = muzik_siralari.setdefault(guild_id, MuzikSirasi())
    if not getattr(sira, "_kuruldu", False):
        sira._kuruldu = True
        sira.dongu = int(_veri.get("dongu", {}).get(str(guild_id), 0))
        sira.autoplay = bool(_veri.get("autoplay", {}).get(str(guild_id), False))
    return sira


def _sira_pozisyonu(sira: MuzikSirasi) -> float:
    """Çalan/duraklatılmış şarkının saniye cinsinden konumu (karaoke senkronu için)."""
    if sira.simdi_calan is None or sira.baslama_zamani is None:
        return 0.0
    if sira.duraklatma_an is not None:
        return max(0.0, sira.duraklatma_an - sira.baslama_zamani - sira.toplam_duraklatma)
    return max(0.0, time.time() - sira.baslama_zamani - sira.toplam_duraklatma)


# ============================================
# 7/24 SESSİZ SES KANALINDA BEKLEME
# ============================================
# Bot, sunucudaki en üstteki (ilk) ses kanalına girer, kendini sessize alır
# (self-mute + self-deaf) ve orada kalır. Biri onu atarsa/kanaldan çıkarsa
# otomatik olarak geri bağlanır. Müzik çalınca oraya taşınır, müzik bitince
# tekrar sabit kanala döner. Kullanıcı /724aktif ile kanal belirlediyse o kanal,
# belirlemediyse en üstteki ses kanalı kullanılır.
async def _sabit_ses_kanaline_baglan(guild: discord.Guild, zorla_tasi: bool = False) -> None:
    # 1) Kullanıcının /724aktif ile belirlediği kanal varsa onu kullan
    hedef = None
    ayar_id = _veri.get("sabit_kanal", {}).get(str(guild.id))
    if ayar_id:
        kanal = guild.get_channel(int(ayar_id))
        if isinstance(kanal, discord.VoiceChannel) and kanal.permissions_for(guild.me).connect:
            hedef = kanal

    # 2) Yoksa en üstteki erişilebilir ses kanalına düş (eski davranış)
    if hedef is None:
        erisilebilir = sorted(
            (vc for vc in guild.voice_channels if vc.permissions_for(guild.me).connect),
            key=lambda c: c.position,
        )
        if not erisilebilir:
            return
        hedef = erisilebilir[0]

    ses_client = discord.utils.get(bot.voice_clients, guild=guild)

    try:
        if ses_client is None or not ses_client.is_connected():
            await hedef.connect(self_mute=True, self_deaf=True, reconnect=True)
        elif ses_client.channel.id != hedef.id:
            # Müzik çalıyorsa/duraklatılmışsa, zorla_tasi=True verilmediği sürece rahatsız etme.
            if zorla_tasi or not (ses_client.is_playing() or ses_client.is_paused()):
                await ses_client.move_to(hedef)
    except discord.HTTPException as e:
        print(f"Sabit ses kanalına bağlanılamadı ({guild.name}): {e}")


def _sarki_ara(sorgu: str) -> dict:
    """Bloklayıcı yt-dlp aramasını çalıştırır (executor içinde çağrılmalı).
    Birden fazla YouTube client'ı dener; başarılı olanın ilk sonucunu döner."""
    bilgi = _sarki_ara_ayarla(YTDLP_AYARLARI, sorgu)
    if "entries" in bilgi:
        girisler = bilgi["entries"] or []
        if not girisler:
            raise RuntimeError(f"'{sorgu}' için YouTube'da sonuç bulunamadı.")
        bilgi = girisler[0]
    return bilgi


def _sarki_ara_ayarla(ayarlar: dict, sorgu: str) -> dict:
    """Belirli ayarlarla bloklayıcı yt-dlp araması çalıştırır; başarısız olursa
    farklı YouTube client'larıyla sırayla dener (datacenter IP'lerinde bazı
    client'lar 'Video unavailable' dönebildiği için)."""
    dene_clientlar = [None]  # None = ayarlardaki mevcut client'lar
    if "youtube" in ayarlar.get("extractor_args", {}):
        mevcut = ayarlar["extractor_args"]["youtube"].get("player_client", [])
        dene_clientlar += [
            ["web", "web_embedded"],
            ["android", "tv"],
            ["mweb"],
        ]
    son_hata = None
    for clientlar in dene_clientlar:
        deneme_ayar = dict(ayarlar)
        if clientlar is not None:
            deneme_ayar = json.loads(json.dumps(deneme_ayar))
            deneme_ayar["extractor_args"] = {
                "youtube": {"player_client": clientlar}
            }
        try:
            with yt_dlp.YoutubeDL(deneme_ayar) as ydl:
                return ydl.extract_info(sorgu, download=False)
        except Exception as e:
            son_hata = e
            continue
    raise son_hata


def _sure_metni(saniye) -> str:
    """Saniye değerini m:ss veya h:mm:ss biçimine çevirir."""
    if not saniye:
        return ""
    saniye = int(saniye)
    saat, kalan = divmod(saniye, 3600)
    dk, sn = divmod(kalan, 60)
    if saat:
        return f"{saat}:{dk:02d}:{sn:02d}"
    return f"{dk}:{sn:02d}"


def _json3_sozler(icerik: str) -> list[dict]:
    """YouTube'un json3 altyazı formatından zaman damgalı söz satırlarını çıkarır."""
    veri = json.loads(icerik)
    satirlar = []
    for olay in veri.get("events", []):
        segler = olay.get("segs") or []
        if not segler:
            continue
        metin = "".join(s.get("utf8", "") for s in segler)
        metin = re.sub(r"<[^>]+>", "", metin).strip()
        if not metin:
            continue
        bas = olay.get("tStartMs", 0) / 1000
        bit = bas + olay.get("dDurationMs", 0) / 1000
        satirlar.append({"start": bas, "end": bit, "metin": metin})
    return satirlar


def _vtt_sozler(icerik: str) -> list[dict]:
    """VTT altyazı formatından zaman damgalı söz satırlarını çıkarır."""
    satirlar = []
    zaman_re = re.compile(
        r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[.,](\d{3})"
    )

    def sn(a, b, c, d):
        return int(a) * 3600 + int(b) * 60 + int(c) + int(d) / 1000

    for blok in re.split(r"\n{2,}", icerik):
        satirlar_liste = blok.strip().splitlines()
        if not satirlar_liste:
            continue
        es = zaman_re.search(satirlar_liste[0])
        if not es:
            continue
        g = es.groups()
        bas = sn(*g[:4])
        bit = sn(*g[4:])
        metin = " ".join(satirlar_liste[1:])
        metin = re.sub(r"<[^>]+>", "", metin).strip()
        if metin:
            satirlar.append({"start": bas, "end": bit, "metin": metin})
    return satirlar


def _sozleri_cek(sorgu: str) -> dict:
    """Bloklayıcı: yt-dlp ile video bilgisini ve YouTube altyazılarını çeker,
    zaman damgalı söz satırlarına çevirir (executor içinde çağrılmalı).
    Döner: {"baslik", "sanatci", "kapak", "sozler"}"""
    ayarlar = dict(YTDLP_AYARLARI)
    ayarlar.pop("format", None)  # söz çekmede ses formatı gerekmez
    ayarlar.update({"skip_download": True})
    bilgi = _sarki_ara_ayarla(ayarlar, sorgu)
    if "entries" in bilgi:
        girisler = bilgi["entries"] or []
        if not girisler:
            raise RuntimeError("Bu şarkı için söz bulunamadı.")
        bilgi = girisler[0]

    manuel = bilgi.get("subtitles") or {}
    otomatik = bilgi.get("automatic_captions") or {}
    if "live_chat" in manuel:
        manuel.pop("live_chat")

    def _track_sec(diller: dict) -> tuple | None:
        """Dil önceliğine göre (tr > en > ilk) bir altyazı track'i seçer.
        YouTube 'tr-XXXX' gibi ekli dil kodları kullanır."""
        adaylar = []
        for d in ("tr", "tr-TR", "en", "en-US"):
            for anahtar, trackler in diller.items():
                if anahtar == d or anahtar.startswith(d + "-"):
                    if trackler:
                        adaylar.append((anahtar, trackler))
                    break
        if not adaylar and diller:
            ilk = next(iter(diller.items()))
            if ilk[1]:
                adaylar.append(ilk)
        for anahtar, trackler in adaylar:
            # json3 tercih, yoksa vtt; url'si olan ilk track
            for tercih in ("json3", "vtt"):
                for t in trackler:
                    if t.get("ext") == tercih and t.get("url"):
                        return anahtar, t
            for t in trackler:
                if t.get("url"):
                    return anahtar, t
        return None

    secim = _track_sec(manuel) or _track_sec(otomatik)
    if secim is None:
        raise RuntimeError("Bu şarkı için zaman damgalı söz bulunamadı.")

    track = secim[1]
    icerik = ""
    with urllib.request.urlopen(track["url"], timeout=15) as yanit:
        icerik = yanit.read().decode("utf-8", "replace")

    sozler = []
    if icerik.strip().startswith("{"):
        sozler = _json3_sozler(icerik)
    else:
        sozler = _vtt_sozler(icerik)
    if not sozler:
        raise RuntimeError("Bu şarkı için zaman damgalı söz bulunamadı.")

    # Son satırın bitişini şarkının toplam süresine kadar uzat.
    sure = bilgi.get("duration")
    if sure and sozler:
        sozler[-1]["end"] = max(sozler[-1]["end"], float(sure))

    return {
        "baslik": bilgi.get("title", ""),
        "sanatci": bilgi.get("channel") or bilgi.get("uploader") or bilgi.get("artist") or "",
        "kapak": bilgi.get("thumbnail") or "",
        "sozler": sozler,
    }


_sozler_cache: dict[str, dict] = {}


def _yt_hata_cevir(hata: Exception) -> str:
    """yt-dlp hatalarını kullanıcı dostu Türkçe mesaja çevirir."""
    metin = str(hata)
    if "Sign in to confirm you're not a bot" in metin or "not a bot" in metin or "cookies" in metin.lower():
        if COOKIES_VAR:
            return (
                "YouTube bot kontrolüne takıldı. **cookies.txt geçersiz/süresi dolmuş** olabilir; "
                "YouTube'a tekrar giriş yapıp taze cookies.txt export edip repoya yüklemen gerekiyor."
            )
        return (
            "YouTube, Railway'in IP'sini bot olarak görüyor. Çözüm: YouTube'a giriş yapılmış "
            "hesaptan **cookies.txt** export edip repo köküne yükle (bot otomatik bulur)."
        )
    if "DRM" in metin or "format is not available" in metin:
        return "Bu şarkı korumalı (DRM) ya da botun çekebileceği bir formatta değil."
    return metin


def _sarki_bitince(guild: discord.Guild, hata: Exception | None):
    """FFmpeg oynatma bitince (ayrı bir thread'den) çağrılır; sıradaki şarkıyı başlatır."""
    if hata:
        # Taşınma/bağlantı kesintisi gibi hatalarda sırayı bozma: şarkıyı atlama.
        print(f"Oynatma hatası (sıra korunuyor): {hata}")
        return
    sira = _sira_al(guild.id)
    # Şarkı döngüsü: şarkı doğal olarak bitince (skip değilse) aynısını en başa koy.
    if sira.dongu == 1 and sira.simdi_calan is not None and not sira.dongu_atlama:
        sira.kuyruk.insert(0, sira.simdi_calan)
    sira.dongu_atlama = False
    fut = asyncio.run_coroutine_threadsafe(_sonrakini_cal(guild), bot.loop)
    try:
        fut.result()
    except Exception as e:
        print(f"Sıradaki şarkıya geçerken hata: {e}")


async def _sonrakini_cal(guild: discord.Guild):
    sira = _sira_al(guild.id)
    ses_client = discord.utils.get(bot.voice_clients, guild=guild)

    if ses_client is None or not ses_client.is_connected():
        sira.simdi_calan = None
        return

    # Zaten bir şarkı çalıyorsa/duraklatıldıysa ikinci kez başlatma (çift tetiklenmeyi önler).
    if ses_client.is_playing() or ses_client.is_paused():
        return

    # Web panelinde seçili bir kanal varsa, şarkı çalmadan önce oraya taşın.
    secili_id = _web_secili_kanal.get(guild.id)
    if secili_id and ses_client.channel.id != secili_id:
        hedef = guild.get_channel(secili_id)
        if isinstance(hedef, discord.VoiceChannel) and hedef.permissions_for(guild.me).connect:
            try:
                await ses_client.move_to(hedef)
            except (discord.HTTPException, OSError, asyncio.TimeoutError) as e:
                print(f"Seçili kanala taşınamadı ({guild.name}): {e}")
        else:
            _web_secili_kanal.pop(guild.id, None)

    if not sira.kuyruk:
        # Kuyruk döngüsü: anlık görüntüyü geri yükle.
        if sira.dongu == 2 and sira.dongu_cevir:
            sira.kuyruk.extend(list(sira.dongu_cevir))
        # Autoplay: kuyruk boşalınca son şarkıya benzer birini ara.
        elif sira.autoplay:
            await _autoplay_ekle(guild)
    if not sira.kuyruk:
        sira.simdi_calan = None
        # Çalınacak şarkı kalmadı, botu tekrar sabit 7/24 ses kanalına gönder.
        try:
            await _sabit_ses_kanaline_baglan(guild, zorla_tasi=True)
        except Exception as e:
            print(f"Müzik bitince sabit kanala dönülemedi ({guild.name}): {e}")
        await _panel_sil(guild)
        return

    sonraki = sira.kuyruk.pop(0)

    try:
        loop = asyncio.get_running_loop()
        bilgi = await loop.run_in_executor(None, functools.partial(_sarki_ara, sonraki.sorgu))
        stream_url = bilgi.get("url")
    except Exception as e:
        try:
            await sonraki.kanal.send(f"⚠️ **{sonraki.baslik}** çalınamadı, atlanıyor: {_yt_hata_cevir(e)}")
        except discord.HTTPException:
            pass
        await _sonrakini_cal(guild)
        return

    if not stream_url:
        try:
            await sonraki.kanal.send(f"⚠️ **{sonraki.baslik}** için oynatılabilir kaynak bulunamadı, atlanıyor.")
        except discord.HTTPException:
            pass
        await _sonrakini_cal(guild)
        return

    # Geçmiş: eski çalan şarkıyı listeye ekle (en fazla 25).
    if sira.simdi_calan is not None:
        sira.gecmis.append(sira.simdi_calan)
        if len(sira.gecmis) > 25:
            sira.gecmis.pop(0)

    sira.simdi_calan = sonraki
    sira.dongu_atlama = False
    seviye = _veri.get("ses_seviyesi", {}).get(str(guild.id), 80)
    options = FFMPEG_SECENEKLERI["options"]
    if 0 <= seviye <= 100 and seviye != 80:
        options = f'{options} -af volume={seviye / 100:.2f}'
    kaynak = discord.FFmpegPCMAudio(stream_url, executable=FFMPEG_YOLU, before_options=FFMPEG_SECENEKLERI["before_options"], options=options)
    sira.baslama_zamani = time.time()
    sira.duraklatma_an = None
    sira.toplam_duraklatma = 0.0
    ses_client.play(kaynak, after=lambda e, g=guild: _sarki_bitince(g, e))

    await _panel_gonder(guild, sonraki)


async def _autoplay_ekle(guild: discord.Guild):
    """Kuyruk boşalınca son çalınan şarkıya benzer bir şarkı arar ve kuyruğa ekler."""
    sira = _sira_al(guild.id)
    onceki = sira.gecmis[-1] if sira.gecmis else sira.simdi_calan
    if onceki is None:
        return
    adaylar: list[dict] = []
    try:
        vid = None
        m = re.search(r"[?&]v=([\w-]{11})", onceki.sorgu or "")
        if m:
            vid = m.group(1)
        if vid:
            ayarlar = dict(YTDLP_AYARLARI)
            ayarlar["extract_flat"] = "in_playlist"
            try:
                bilgi = await asyncio.get_running_loop().run_in_executor(
                    None, functools.partial(_sarki_ara_ayarla, ayarlar, f"https://www.youtube.com/watch?v={vid}&list=RD{vid}")
                )
                for giris in (bilgi.get("entries") or [])[:8]:
                    if not giris or not giris.get("title"):
                        continue
                    url = giris.get("url") or ""
                    if not url.startswith("http"):
                        url = f"https://www.youtube.com/watch?v={url}"
                    adaylar.append({"baslik": giris["title"], "sorgu": url,
                                    "thumbnail": giris.get("thumbnail"),
                                    "sure": _sure_metni(giris.get("duration"))})
            except Exception:
                adaylar = []
    except Exception:
        pass
    if not adaylar:
        # Yedek: başlıkla arama yap, aynı şarkıyı seçme.
        try:
            bilgi = await asyncio.get_running_loop().run_in_executor(
                None, functools.partial(_sarki_ara, f"ytsearch5 {onceki.baslik}")
            )
            entries = bilgi.get("entries") or []
            for giris in entries[:5]:
                if giris and giris.get("title"):
                    adaylar.append({"baslik": giris["title"], "sorgu": giris.get("webpage_url", ""),
                                    "thumbnail": giris.get("thumbnail"),
                                    "sure": _sure_metni(giris.get("duration"))})
        except Exception:
            pass
    secilen = next((a for a in adaylar if a["sorgu"] and a["sorgu"] != onceki.sorgu), None)
    if secilen is None:
        return
    kayit = SarkiKaydi(sorgu=secilen["sorgu"], baslik=secilen["baslik"], isteyen=guild.me,
                       kanal=onceki.kanal, thumbnail=secilen.get("thumbnail"), sure=secilen.get("sure"))
    sira.kuyruk.append(kayit)
    print(f"[AUTOPLAY] {guild.name}: {secilen['baslik']} eklendi.")


# ============================================
# DISCORD'DA BUTONLU "ŞİMDİ ÇALIYOR" KARTI
# ============================================
_web_panel_mesajlari: dict[int, discord.Message] = {}  # guild_id -> butonlu kart mesajı


def _dongu_etiket(dongu: int) -> str:
    return {0: "Döngü: Yok", 1: "Döngü: Şarkı", 2: "Döngü: Kuyruk"}.get(dongu, "Döngü: Yok")


class SimdiCaliyorView(discord.ui.View):
    def __init__(self, guild: discord.Guild):
        super().__init__(timeout=None)
        self.guild = guild

    async def _hareket(self, interaction: discord.Interaction, islem):
        if interaction.guild is None:
            return
        await interaction.response.defer()
        try:
            await islem(interaction.guild)
        except Exception as e:
            print(f"Panel butonu hatası ({interaction.guild.name}): {e}")
        await _panel_guncelle(interaction.guild)

    @discord.ui.button(emoji="⏮️", label="Önceki", style=discord.ButtonStyle.secondary, custom_id="dvrms_panel_prev")
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        async def islem(guild):
            sira = _sira_al(guild.id)
            if sira.gecmis and sira.simdi_calan is not None:
                geri = sira.gecmis.pop()
                sira.kuyruk.insert(0, geri)
                sira.dongu_atlama = True
                ses_client = discord.utils.get(bot.voice_clients, guild=guild)
                if ses_client is not None and (ses_client.is_playing() or ses_client.is_paused()):
                    ses_client.stop()
        await self._hareket(interaction, islem)

    @discord.ui.button(emoji="⏸️", label="Duraklat", style=discord.ButtonStyle.secondary, custom_id="dvrms_panel_pause")
    async def pause(self, interaction: discord.Interaction, button: discord.ui.Button):
        async def islem(guild):
            sira = _sira_al(guild.id)
            ses_client = discord.utils.get(bot.voice_clients, guild=guild)
            if ses_client is None:
                return
            if ses_client.is_playing():
                sira.duraklatma_an = time.time()
                ses_client.pause()
            elif ses_client.is_paused():
                if sira.duraklatma_an is not None:
                    sira.toplam_duraklatma += time.time() - sira.duraklatma_an
                    sira.duraklatma_an = None
                ses_client.resume()
        await self._hareket(interaction, islem)

    @discord.ui.button(emoji="⏭️", label="Atla", style=discord.ButtonStyle.secondary, custom_id="dvrms_panel_skip")
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        async def islem(guild):
            sira = _sira_al(guild.id)
            ses_client = discord.utils.get(bot.voice_clients, guild=guild)
            if ses_client is not None and (ses_client.is_playing() or ses_client.is_paused()):
                sira.dongu_atlama = True
                ses_client.stop()
        await self._hareket(interaction, islem)

    @discord.ui.button(emoji="⏹️", label="Durdur", style=discord.ButtonStyle.danger, custom_id="dvrms_panel_stop")
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        async def islem(guild):
            sira = _sira_al(guild.id)
            sira.kuyruk.clear()
            sira.simdi_calan = None
            sira.baslama_zamani = None
            sira.duraklatma_an = None
            sira.toplam_duraklatma = 0.0
            ses_client = discord.utils.get(bot.voice_clients, guild=guild)
            if ses_client is not None:
                if ses_client.is_playing() or ses_client.is_paused():
                    ses_client.stop()
                try:
                    await ses_client.disconnect()
                except discord.HTTPException:
                    pass
            await _panel_sil(guild)
        await self._hareket(interaction, islem)

    @discord.ui.button(emoji="🔀", label="Karıştır", style=discord.ButtonStyle.secondary, custom_id="dvrms_panel_shuffle")
    async def shuffle(self, interaction: discord.Interaction, button: discord.ui.Button):
        async def islem(guild):
            sira = _sira_al(guild.id)
            if len(sira.kuyruk) > 1:
                random.shuffle(sira.kuyruk)
        await self._hareket(interaction, islem)

    @discord.ui.button(emoji="🔁", label="Döngü", style=discord.ButtonStyle.secondary, custom_id="dvrms_panel_loop")
    async def loop(self, interaction: discord.Interaction, button: discord.ui.Button):
        async def islem(guild):
            sira = _sira_al(guild.id)
            sira.dongu = (sira.dongu + 1) % 3
            _veri.setdefault("dongu", {})[str(guild.id)] = sira.dongu
            if sira.dongu == 2:
                sira.dongu_cevir = list(sira.kuyruk)
            elif sira.dongu == 0:
                sira.dongu_cevir = []
            _veri_kaydet()
        await self._hareket(interaction, islem)


def _panel_embed(guild: discord.Guild) -> discord.Embed | None:
    sira = _sira_al(guild.id)
    ses_client = discord.utils.get(bot.voice_clients, guild=guild)
    if sira.simdi_calan is None:
        return None
    s = sira.simdi_calan
    durum = "Duraklatıldı" if (ses_client and ses_client.is_paused()) else "Çalıyor"
    embed = discord.Embed(
        title=f"🎵 {durum}: {s.baslik}",
        description=f"İstek: {s.isteyen.mention}\nSüre: {s.sure or '—'} · {_dongu_etiket(sira.dongu)}"
                    + (" · 🔁 Autoplay" if sira.autoplay else ""),
        color=discord.Color.purple(),
    )
    if s.thumbnail:
        embed.set_thumbnail(url=s.thumbnail)
    embed.set_footer(text=f"{guild.name} · DVRM Müzik Paneli")
    return embed


async def _panel_gonder(guild: discord.Guild, sonraki: SarkiKaydi | None = None):
    """Şimdi çalıyor kartını gönderir; varsa mevcut mesajı düzenler."""
    sira = _sira_al(guild.id)
    if sira.simdi_calan is None:
        await _panel_sil(guild)
        return
    kanal = (sonraki or sira.simdi_calan).kanal
    if not isinstance(kanal, discord.TextChannel):
        return
    embed = _panel_embed(guild)
    if embed is None:
        await _panel_sil(guild)
        return
    mevcut = _web_panel_mesajlari.get(guild.id)
    if mevcut is not None:
        try:
            await mevcut.edit(embed=embed, view=SimdiCaliyorView(guild))
            return
        except discord.HTTPException:
            pass
    try:
        mesaj = await kanal.send(embed=embed, view=SimdiCaliyorView(guild))
        _web_panel_mesajlari[guild.id] = mesaj
    except discord.HTTPException:
        pass


async def _panel_guncelle(guild: discord.Guild):
    """Butonlu karttaki embed ve buton etiketlerini günceller (duraklat/devam/döngü vb.)."""
    mevcut = _web_panel_mesajlari.get(guild.id)
    if mevcut is None:
        return
    sira = _sira_al(guild.id)
    if sira.simdi_calan is None:
        await _panel_sil(guild)
        return
    embed = _panel_embed(guild)
    if embed is None:
        await _panel_sil(guild)
        return
    try:
        await mevcut.edit(embed=embed, view=SimdiCaliyorView(guild))
    except discord.HTTPException:
        _web_panel_mesajlari.pop(guild.id, None)


async def _panel_sil(guild: discord.Guild):
    mevcut = _web_panel_mesajlari.pop(guild.id, None)
    if mevcut is not None:
        try:
            await mevcut.delete()
        except discord.HTTPException:
            pass


@bot.tree.command(name="sil", description="Bu kanalda belirtilen sayıda son mesajı siler.")
@app_commands.describe(miktar="Silinecek mesaj sayısı (1-100 arası)")
@app_commands.checks.has_permissions(manage_messages=True)
async def sil(interaction: discord.Interaction, miktar: app_commands.Range[int, 1, 100]):
    if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("Bu komut sadece sunucudaki bir metin kanalında kullanılabilir.", ephemeral=True)
        return

    if not interaction.channel.permissions_for(interaction.guild.me).manage_messages:
        await interaction.response.send_message("Botun 'Mesajları Yönet' iznine ihtiyacım var.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    # Discord'un toplu silme (bulk delete) API'si 14 günden eski mesajları kabul etmez.
    # Bu yüzden önce hedef mesajları çekip 14 günden yeni/eski diye ikiye ayırıyoruz:
    # yeniler purge (toplu) ile, eskiler ise tek tek delete() ile silinir.
    on_dort_gun_once = discord.utils.utcnow() - datetime.timedelta(days=14)

    try:
        mesajlar = [m async for m in interaction.channel.history(limit=miktar)]
    except discord.HTTPException as e:
        await interaction.followup.send(f"Mesajlar okunamadı: {e}", ephemeral=True)
        return

    yeni_mesajlar = [m for m in mesajlar if m.created_at >= on_dort_gun_once]
    eski_mesajlar = [m for m in mesajlar if m.created_at < on_dort_gun_once]

    toplam_silinen = 0

    if yeni_mesajlar:
        try:
            silinenler = await interaction.channel.purge(limit=len(yeni_mesajlar))
            toplam_silinen += len(silinenler)
        except discord.HTTPException as e:
            await interaction.followup.send(f"Mesajlar silinemedi: {e}", ephemeral=True)
            return

    for mesaj in eski_mesajlar:
        try:
            await mesaj.delete()
            toplam_silinen += 1
            await asyncio.sleep(1)  # rate limit'e takılmamak için mesaj başına küçük bekleme
        except discord.HTTPException:
            pass

    await interaction.followup.send(
        f"🗑️ {toplam_silinen} mesaj silindi"
        + (f" ({len(eski_mesajlar)} tanesi 14 günden eski olduğu için tek tek silindi)." if eski_mesajlar else "."),
        ephemeral=True,
    )


@bot.tree.command(name="play", description="Şarkıyı kuyruğa ekler; sırada bir şey yoksa hemen çalar.")
@app_commands.describe(sarki="Çalınacak şarkının adı veya YouTube linki")
async def play(interaction: discord.Interaction, sarki: str):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return

    if interaction.user.voice is None or interaction.user.voice.channel is None:
        await interaction.response.send_message(
            "Önce bir ses kanalına girmen lazım ki bot yanına gelebilsin.", ephemeral=True
        )
        return

    await interaction.response.defer()

    kanal = interaction.user.voice.channel
    ses_client = discord.utils.get(bot.voice_clients, guild=interaction.guild)

    if ses_client is None:
        try:
            ses_client = await kanal.connect()
        except discord.ClientException as e:
            await interaction.followup.send(f"Kanala bağlanamadım: {e}")
            return
    elif ses_client.channel.id != kanal.id:
        await ses_client.move_to(kanal)

    try:
        loop = asyncio.get_running_loop()
        bilgi = await loop.run_in_executor(None, functools.partial(_sarki_ara, sarki))
    except Exception as e:
        await interaction.followup.send(f"Şarkı çalınamadı: {_yt_hata_cevir(e)}")
        return

    baslik = bilgi.get("title", sarki)
    sorgu = bilgi.get("webpage_url", sarki)
    thumbnail = bilgi.get("thumbnail")
    sure = _sure_metni(bilgi.get("duration"))

    sira = _sira_al(interaction.guild.id)
    kayit = SarkiKaydi(sorgu=sorgu, baslik=baslik, isteyen=interaction.user, kanal=interaction.channel,
                       thumbnail=thumbnail, sure=sure, sure_sn=bilgi.get("duration"))
    sira.kuyruk.append(kayit)

    if sira.simdi_calan is None and not ses_client.is_playing() and not ses_client.is_paused():
        await interaction.followup.send(f"🎵 Kuyruğa eklendi, hemen başlıyor: **{baslik}**")
        await _sonrakini_cal(interaction.guild)
    else:
        sira_no = len(sira.kuyruk)
        await interaction.followup.send(f"➕ Kuyruğa eklendi: **{baslik}** (sırada {sira_no}. konumda)")


@bot.tree.command(name="skip", description="Çalan şarkıyı atlar, sıradaki varsa onu çalar.")
async def skip(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return

    ses_client = discord.utils.get(bot.voice_clients, guild=interaction.guild)
    sira = _sira_al(interaction.guild.id)

    if ses_client is None or sira.simdi_calan is None:
        await interaction.response.send_message("Şu anda çalan bir şarkı yok.", ephemeral=True)
        return

    baslik = sira.simdi_calan.baslik
    sira.dongu_atlama = True  # şarkı döngüsü açıksa skip'te yeniden başlatmayı bastır
    ses_client.stop()  # after callback tetiklenip otomatik olarak sıradakine geçer
    await interaction.response.send_message(f"⏭️ **{baslik}** atlandı.")


@bot.tree.command(name="sira", description="Çalma kuyruğunu gösterir.")
async def sira_goster(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return

    sira = _sira_al(interaction.guild.id)

    if sira.simdi_calan is None and not sira.kuyruk:
        await interaction.response.send_message("Kuyrukta hiçbir şey yok.", ephemeral=True)
        return

    satirlar = []
    if sira.simdi_calan is not None:
        satirlar.append(f"🎵 **Şimdi çalıyor:** {sira.simdi_calan.baslik} — istek: {sira.simdi_calan.isteyen.mention}")
    if sira.kuyruk:
        satirlar.append("")
        satirlar.append("**Sırada:**")
        for i, kayit in enumerate(sira.kuyruk[:10], start=1):
            satirlar.append(f"{i}. {kayit.baslik} — istek: {kayit.isteyen.mention}")
        if len(sira.kuyruk) > 10:
            satirlar.append(f"...ve {len(sira.kuyruk) - 10} şarkı daha.")

    await interaction.response.send_message("\n".join(satirlar))


@bot.tree.command(name="kuyruktemizle", description="Sıradaki şarkıları temizler (çalan şarkı devam eder).")
async def kuyruktemizle(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return

    sira = _sira_al(interaction.guild.id)
    adet = len(sira.kuyruk)
    sira.kuyruk.clear()
    await interaction.response.send_message(f"🗑️ Kuyruktan {adet} şarkı temizlendi.")


@bot.tree.command(name="duraklat", description="Çalan şarkıyı duraklatır.")
async def duraklat(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return

    ses_client = discord.utils.get(bot.voice_clients, guild=interaction.guild)
    if ses_client is None or not ses_client.is_playing():
        await interaction.response.send_message("Şu anda çalan bir şarkı yok.", ephemeral=True)
        return

    ses_client.pause()
    sira = _sira_al(interaction.guild.id)
    sira.duraklatma_an = time.time()
    await interaction.response.send_message("⏸️ Duraklatıldı.")
    await _panel_guncelle(interaction.guild)


@bot.tree.command(name="devam", description="Duraklatılmış şarkıyı devam ettirir.")
async def devam(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return

    ses_client = discord.utils.get(bot.voice_clients, guild=interaction.guild)
    if ses_client is None or not ses_client.is_paused():
        await interaction.response.send_message("Duraklatılmış bir şarkı yok.", ephemeral=True)
        return

    ses_client.resume()
    sira = _sira_al(interaction.guild.id)
    if sira.duraklatma_an is not None:
        sira.toplam_duraklatma += time.time() - sira.duraklatma_an
        sira.duraklatma_an = None
    await interaction.response.send_message("▶️ Devam ediyor.")
    await _panel_guncelle(interaction.guild)


@bot.tree.command(name="dur", description="Çalan müziği durdurur, kuyruğu temizler ve bot ses kanalından ayrılır.")
async def dur(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return

    sira = _sira_al(interaction.guild.id)
    sira.kuyruk.clear()
    sira.simdi_calan = None

    ses_client = discord.utils.get(bot.voice_clients, guild=interaction.guild)
    if ses_client is None:
        await interaction.response.send_message("Zaten hiçbir ses kanalında değilim.", ephemeral=True)
        return

    if ses_client.is_playing() or ses_client.is_paused():
        ses_client.stop()
    await ses_client.disconnect()

    await _panel_sil(interaction.guild)

    await interaction.response.send_message("⏹️ Müzik durduruldu, kuyruk temizlendi, ses kanalından ayrıldım.")


@bot.tree.command(name="dongu", description="Döngü modunu ayarlar: yok / şarkı / kuyruk.")
@app_commands.describe(mod="Döngü modu: yok, sarki veya kuyruk")
@app_commands.choices(mod=[
    app_commands.Choice(name="Yok", value=0),
    app_commands.Choice(name="Şarkı", value=1),
    app_commands.Choice(name="Kuyruk", value=2),
])
async def dongu(interaction: discord.Interaction, mod: app_commands.Choice[int]):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return
    sira = _sira_al(interaction.guild.id)
    sira.dongu = mod.value
    _veri.setdefault("dongu", {})[str(interaction.guild.id)] = mod.value
    if mod.value == 2:
        sira.dongu_cevir = list(sira.kuyruk)
    elif mod.value == 0:
        sira.dongu_cevir = []
    _veri_kaydet()
    await interaction.response.send_message(f"🔁 Döngü: **{mod.name}**")
    await _panel_guncelle(interaction.guild)


@bot.tree.command(name="karistir", description="Sıradaki şarkıları karıştırır.")
async def karistir(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return
    sira = _sira_al(interaction.guild.id)
    if len(sira.kuyruk) < 2:
        await interaction.response.send_message("Karıştırmak için sırada en az 2 şarkı olmalı.", ephemeral=True)
        return
    random.shuffle(sira.kuyruk)
    await interaction.response.send_message(f"🔀 Kuyruktaki {len(sira.kuyruk)} şarkı karıştırıldı.")


@bot.tree.command(name="gecmis", description="Son çalınan şarkıları gösterir.")
async def gecmis(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return
    sira = _sira_al(interaction.guild.id)
    if not sira.gecmis:
        await interaction.response.send_message("Henüz çalınmış şarkı yok.", ephemeral=True)
        return
    satirlar = ["**Son çalınanlar:**"]
    for i, kayit in enumerate(reversed(sira.gecmis[-10:]), start=1):
        satirlar.append(f"{i}. {kayit.baslik} — istek: {kayit.isteyen.name}")
    await interaction.response.send_message("\n".join(satirlar))


@bot.tree.command(name="gerial", description="Son çalınan şarkıyı tekrar çalar (geçmişten geri alır).")
async def gerial(interaction: discord.Interaction):
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return
    if interaction.user.voice is None or interaction.user.voice.channel is None:
        await interaction.response.send_message("Önce bir ses kanalına girmen lazım.", ephemeral=True)
        return
    sira = _sira_al(interaction.guild.id)
    if not sira.gecmis:
        await interaction.response.send_message("Geçmişte çalınmış şarkı yok.", ephemeral=True)
        return
    geri = sira.gecmis.pop()
    sira.kuyruk.insert(0, geri)
    ses_client = discord.utils.get(bot.voice_clients, guild=interaction.guild)
    if ses_client is None:
        try:
            ses_client = await interaction.user.voice.channel.connect()
        except discord.ClientException as e:
            await interaction.response.send_message(f"Kanala bağlanamadım: {e}")
            return
    elif ses_client.channel.id != interaction.user.voice.channel.id:
        await ses_client.move_to(interaction.user.voice.channel)
    if ses_client.is_playing() or ses_client.is_paused():
        sira.dongu_atlama = True
        ses_client.stop()  # sıradakine geçince geri alınan şarkı çalacak
        await interaction.response.send_message(f"⏮️ Geçmişten geri alındı: **{geri.baslik}**")
    else:
        await interaction.response.send_message(f"⏮️ Geçmişten çalıyor: **{geri.baslik}**")
        await _sonrakini_cal(interaction.guild)


@bot.tree.command(name="autoplay", description="Kuyruk bitince benzer şarkıların otomatik çalmasını açar/kapatır.")
async def autoplay(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return
    sira = _sira_al(interaction.guild.id)
    sira.autoplay = not sira.autoplay
    _veri.setdefault("autoplay", {})[str(interaction.guild.id)] = sira.autoplay
    _veri_kaydet()
    if sira.autoplay:
        await interaction.response.send_message("🔁 Autoplay açıldı. Kuyruk bitince benzer şarkılar otomatik çalacak.")
    else:
        await interaction.response.send_message("🔁 Autoplay kapatıldı.")
    await _panel_guncelle(interaction.guild)


@bot.tree.command(name="724aktif", description="Bulunduğun ses kanalını 7/24 sabit kanal yapar, botu oraya gönderir.")
@app_commands.checks.has_permissions(manage_guild=True)
async def yedi_yirmi_dort_aktif(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return

    if not isinstance(interaction.user, discord.Member) or interaction.user.voice is None or interaction.user.voice.channel is None:
        await interaction.response.send_message("Önce bir ses kanalına girmen lazım.", ephemeral=True)
        return

    kanal = interaction.user.voice.channel
    _veri["sabit_kanal"][str(interaction.guild.id)] = str(kanal.id)
    _veri_kaydet()

    try:
        await _sabit_ses_kanaline_baglan(interaction.guild, zorla_tasi=True)
    except Exception as e:
        await interaction.response.send_message(f"Kanal kaydedildi ama bağlanılamadı: {e}", ephemeral=True)
        return

    await interaction.response.send_message(
        f"✅ **{kanal.name}** artık 7/24 sabit kanal. Bot orada bekleyecek, "
        f"müzik bitince hep buraya dönecek."
    )


# ============================================
# MODERASYON: UYARI / KICK / BAN / LOG KANALI
# ============================================

@bot.tree.command(name="uyari", description="Kullanıcıya uyarı ekler.")
@app_commands.describe(user="Uyarılacak kullanıcı", sebep="Uyarı sebebi (opsiyonel)")
@app_commands.checks.has_permissions(moderate_members=True)
async def uyari(interaction: discord.Interaction, user: discord.Member, sebep: str = "Belirtilmedi"):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return

    anahtar = f"{interaction.guild.id}:{user.id}"
    uyarilar = _veri["uyarilar"].setdefault(anahtar, [])
    uyarilar.append({
        "zaman": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "sebep": sebep,
        "yetkili": str(interaction.user),
    })
    _veri_kaydet()

    await interaction.response.send_message(
        f"⚠️ {user.mention} uyarıldı (**{len(uyarilar)}. uyarı**). Sebep: {sebep}"
    )
    await _log_gonder(
        interaction.guild,
        "⚠️ Uyarı Verildi",
        f"{user.mention} uyarıldı.\nSebep: **{sebep}**\nUyarı sayısı: **{len(uyarilar)}**\nYetkili: {interaction.user.mention}",
        discord.Color.orange(),
    )


@bot.tree.command(name="uyarilar", description="Kullanıcının uyarılarını listeler.")
@app_commands.describe(user="Uyarıları görülecek kullanıcı")
@app_commands.checks.has_permissions(moderate_members=True)
async def uyarilar(interaction: discord.Interaction, user: discord.Member):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return

    uyarilar_listesi = _veri["uyarilar"].get(f"{interaction.guild.id}:{user.id}", [])
    if not uyarilar_listesi:
        await interaction.response.send_message(f"{user.mention} kişisinin hiç uyarısı yok.", ephemeral=True)
        return

    satirlar = []
    for i, u in enumerate(uyarilar_listesi, 1):
        satirlar.append(f"**{i}.** {u['sebep']} — yetkili: {u['yetkili']} ({u['zaman'][:16]})")
    await interaction.response.send_message(
        f"⚠️ **{user.mention}** uyarıları ({len(uyarilar_listesi)}):\n" + "\n".join(satirlar[:15]),
        ephemeral=True,
    )


@bot.tree.command(name="uyarisil", description="Kullanıcının belirtilen uyarısını kaldırır.")
@app_commands.describe(user="Uyarısı silinecek kullanıcı", numara="Silinecek uyarı numarası (varsayılan 1)")
@app_commands.checks.has_permissions(moderate_members=True)
async def uyarisil(interaction: discord.Interaction, user: discord.Member, numara: int = 1):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return

    anahtar = f"{interaction.guild.id}:{user.id}"
    uyarilar_listesi = _veri["uyarilar"].get(anahtar, [])
    if not (1 <= numara <= len(uyarilar_listesi)):
        await interaction.response.send_message(
            f"Geçersiz numara. {user.mention} kişisinin {len(uyarilar_listesi)} uyarısı var.",
            ephemeral=True,
        )
        return

    silinen = uyarilar_listesi.pop(numara - 1)
    if not uyarilar_listesi:
        del _veri["uyarilar"][anahtar]
    _veri_kaydet()

    await interaction.response.send_message(
        f"Uyarı #{numara} silindi: **{silinen['sebep']}** ({user.mention})"
    )


@bot.tree.command(name="kick", description="Kullanıcıyı sunucudan atar.")
@app_commands.describe(user="Atılacak kullanıcı", sebep="Sebep (opsiyonel)")
@app_commands.checks.has_permissions(kick_members=True)
async def kick(interaction: discord.Interaction, user: discord.Member, sebep: str = "Belirtilmedi"):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return

    if user == interaction.user:
        await interaction.response.send_message("Kendini atamazsın.", ephemeral=True)
        return
    if user == interaction.guild.me:
        await interaction.response.send_message("Beni atamam.", ephemeral=True)
        return
    if user.top_role >= interaction.guild.me.top_role:
        await interaction.response.send_message(
            f"{user.mention} kullanıcısının rolü benimkinden yüksek/eşit, atamam.", ephemeral=True
        )
        return

    try:
        await user.kick(reason=sebep)
    except discord.HTTPException as e:
        await interaction.response.send_message(f"Atılamadı: {e}", ephemeral=True)
        return

    await interaction.response.send_message(f"👢 {user.mention} sunucudan atıldı. Sebep: **{sebep}**")
    await _log_gonder(
        interaction.guild,
        "👢 Kullanıcı Atıldı",
        f"{user.mention} ({user.name}) sunucudan atıldı.\nSebep: **{sebep}**\nYetkili: {interaction.user.mention}",
        discord.Color.orange(),
    )


@bot.tree.command(name="ban", description="Kullanıcıyı sunucudan yasaklar.")
@app_commands.describe(user="Yasaklanacak kullanıcı", sebep="Sebep (opsiyonel)")
@app_commands.checks.has_permissions(ban_members=True)
async def ban(interaction: discord.Interaction, user: discord.Member, sebep: str = "Belirtilmedi"):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return

    if user == interaction.user:
        await interaction.response.send_message("Kendini yasaklayamazsın.", ephemeral=True)
        return
    if user == interaction.guild.me:
        await interaction.response.send_message("Beni yasaklayamazsın.", ephemeral=True)
        return
    if user.top_role >= interaction.guild.me.top_role:
        await interaction.response.send_message(
            f"{user.mention} kullanıcısının rolü benimkinden yüksek/eşit, yasaklayamam.", ephemeral=True
        )
        return

    try:
        await user.ban(reason=sebep)
    except discord.HTTPException as e:
        await interaction.response.send_message(f"Yasaklanamadı: {e}", ephemeral=True)
        return

    await interaction.response.send_message(f"🔨 {user.mention} sunucudan yasaklandı. Sebep: **{sebep}**")
    await _log_gonder(
        interaction.guild,
        "🔨 Kullanıcı Yasaklandı",
        f"{user.mention} ({user.name}) sunucudan yasaklandı.\nSebep: **{sebep}**\nYetkili: {interaction.user.mention}",
        discord.Color.red(),
    )


@bot.tree.command(name="unban", description="Kullanıcı ID'si ile yasağı kaldırır.")
@app_commands.describe(kullanici_id="Yasağı kaldırılacak kullanıcının ID'si")
@app_commands.checks.has_permissions(ban_members=True)
async def unban(interaction: discord.Interaction, kullanici_id: str):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return

    try:
        kullanici = await bot.fetch_user(int(kullanici_id))
    except (ValueError, discord.NotFound, discord.HTTPException):
        await interaction.response.send_message("Geçersiz kullanıcı ID'si.", ephemeral=True)
        return

    try:
        await interaction.guild.unban(kullanici, reason=f"{interaction.user} yasağı kaldırdı")
    except discord.HTTPException as e:
        await interaction.response.send_message(f"Yasak kaldırılamadı: {e}", ephemeral=True)
        return

    await interaction.response.send_message(f"♻️ {kullanici.mention} yasağı kaldırıldı.")


@bot.tree.command(name="logkanali", description="Olay loglarının gönderileceği kanalı ayarlar.")
@app_commands.describe(kanal="Logların gönderileceği metin kanalı")
@app_commands.checks.has_permissions(manage_guild=True)
async def logkanali(interaction: discord.Interaction, kanal: discord.TextChannel):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return

    _veri["log_kanali"][str(interaction.guild.id)] = str(kanal.id)
    _veri_kaydet()
    await interaction.response.send_message(f"📝 Log kanalı **#{kanal.name}** olarak ayarlandı.")


@bot.tree.command(name="giriscikis", description="Üye giriş/çıkış mesajlarının gönderileceği kanalı ayarlar.")
@app_commands.describe(kanal="Giriş/çıkış mesajlarının gönderileceği metin kanalı")
@app_commands.checks.has_permissions(manage_guild=True)
async def giriscikis(interaction: discord.Interaction, kanal: discord.TextChannel):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return

    _veri["giris_cikis_kanali"][str(interaction.guild.id)] = str(kanal.id)
    _veri_kaydet()
    await interaction.response.send_message(f"📥📤 Giriş/çıkış kanalı **#{kanal.name}** olarak ayarlandı.")


# ============================================
# KORUMA AYARLARI
# ============================================

@bot.tree.command(name="koruma", description="Koruma ayarlarını yönet: link, kufur, spam, yenihesap.")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(
    ozellik="Koruma türü (boş bırakılırsa durum gösterilir)",
    acik="Açık mı kapat mı (True/False)",
)
async def koruma(interaction: discord.Interaction, ozellik: str = None, acik: bool = None):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return

    ayarlar = _veri["koruma"].setdefault(str(interaction.guild.id), {})

    if ozellik is None:
        satirlar = []
        for anahtar, isim in KORUMA_OZELLIKLERI.items():
            durum = "✅ açık" if ayarlar.get(anahtar) else "❌ kapalı"
            satirlar.append(f"**{isim}** — {durum}")
        await interaction.response.send_message("🛡️ **Koruma durumu:**\n" + "\n".join(satirlar))
        return

    if ozellik not in KORUMA_OZELLIKLERI:
        await interaction.response.send_message(
            f"Bilinmeyen tür: **{ozellik}**. Seçenekler: {', '.join(KORUMA_OZELLIKLERI)}",
            ephemeral=True,
        )
        return

    if acik is None:
        durum = "açık ✅" if ayarlar.get(ozellik) else "kapalı ❌"
        await interaction.response.send_message(
            f"**{KORUMA_OZELLIKLERI[ozellik]}** şu an **{durum}**.", ephemeral=True
        )
        return

    ayarlar[ozellik] = acik
    _veri_kaydet()
    durum_metni = "açıldı ✅" if acik else "kapatıldı ❌"
    await interaction.response.send_message(
        f"**{KORUMA_OZELLIKLERI[ozellik]}** koruması {durum_metni}."
    )


# ============================================
# GENEL DUYURU (TOPLU DM)
# ============================================
DUYURU_ISLEM = None  # aktif duyuru: {"guild_id": int, "basladi": float, "kalan": list, "toplam": int}


@bot.tree.command(name="genelduyuru", description="Sunucudaki tüm üyelere DM ile toplu duyuru gönderir.")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(
    mesaj="Gönderilecek duyuru mesajı",
    onay="Onay istemesi için True bırak (varsayılan)",
)
async def genelduyuru(interaction: discord.Interaction, mesaj: str, onay: bool = True):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return

    hedefler = [m for m in interaction.guild.members if not m.bot]
    kapali = _veri.get("duyuru_kapali", {})
    acik_hedfler = [m for m in hedefler if not kapali.get(f"{interaction.guild.id}:{m.id}")]
    if not acik_hedfler:
        await interaction.response.send_message("📭 Gönderilebilecek üye yok.", ephemeral=True)
        return

    ozet = f"📣 **{len(acik_hedfler)}** üyeye DM gönderilecek. (Toplam {len(hedefler)} üye, {len(hedefler) - len(acik_hedfler)} kişi duyuruları kapattı)\n\n📝 **Mesaj:**\n{mesaj}"

    if not onay:
        await interaction.response.defer()
        await _duyuru_gonder(interaction, mesaj, acik_hedfler)
        return

    view = OnayView(
        lambda i: _duyuru_onay(i, mesaj, acik_hedfler),
        lambda i: i.response.edit_message(content="❌ Duyuru iptal edildi.", view=None),
    )
    await interaction.response.send_message(ozet, view=view)


async def _duyuru_onay(interaction: discord.Interaction, mesaj: str, hedefler: list) -> None:
    await interaction.response.edit_message(content="📨 Duyuru gönderiliyor...", view=None)
    await _duyuru_gonder(interaction, mesaj, hedefler)


async def _duyuru_gonder(interaction: discord.Interaction, mesaj: str, hedefler: list) -> None:
    global DUYURU_ISLEM
    DUYURU_ISLEM = {"guild_id": interaction.guild.id, "basladi": time.time(), "kalan": hedefler, "toplam": len(hedefler)}
    basarili = 0
    karsi_tara = interaction.user.name if interaction.user else "Bot"
    embed_metni = f"📣 **{interaction.guild.name}** sunucusundan duyuru:\n\n{mesaj}"

    for uye in hedefler:
        if DUYURU_ISLEM is None or DUYURU_ISLEM.get("guild_id") != interaction.guild.id:
            break
        try:
            await uye.send(embed_metni)
            basarili += 1
        except discord.HTTPException:
            pass
        await asyncio.sleep(0.4)

    DUYURU_ISLEM = None
    durum = "✅" if basarili else "⚠️"
    embed = discord.Embed(
        title=f"{durum} Duyuru tamamlandı",
        description=f"📤 **{basarili}/{len(hedefler)}** üyeye ulaştı.",
        color=discord.Color.green() if basarili else discord.Color.orange(),
    )
    embed.set_footer(text=f"Gönderen: {karsi_tara}")
    await interaction.edit_original_response(embed=embed)


@bot.tree.command(name="duyurukapat", description="Genel duyuruları (DM) kapatır/açar.")
async def duyurukapat(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return
    anahtar = f"{interaction.guild.id}:{interaction.user.id}"
    kapali = _veri.setdefault("duyuru_kapali", {})
    if kapali.pop(anahtar, None):
        durum = "açıldı ✅"
    else:
        kapali[anahtar] = True
        durum = "kapatıldı ❌"
    _veri_kaydet()
    await interaction.response.send_message(f"🔕 Genel duyurular {durum}.")

@bot.tree.command(name="level", description="Seviyeni veya bir kullanıcının seviyesini gösterir.")
@app_commands.describe(user="Seviyesi görülecek kullanıcı (boş = kendin)")
async def level(interaction: discord.Interaction, user: discord.Member = None):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return

    uye = user or interaction.user
    xp = _veri["xp"].get(f"{interaction.guild.id}:{uye.id}", 0)
    seviye, mevcut_xp, gerekli = _seviye_hesapla(xp)

    doluluk = round(10 * (mevcut_xp / gerekli)) if gerekli else 10
    bar = "█" * doluluk + "░" * (10 - doluluk)

    embed = discord.Embed(title=f"{uye.display_name} — Seviye {seviye}", color=discord.Color.gold())
    embed.add_field(name="İlerleme", value=f"{bar} {mevcut_xp}/{gerekli} XP", inline=False)
    embed.add_field(name="Toplam XP", value=str(xp), inline=True)
    embed.set_thumbnail(url=uye.display_avatar.url)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="liderlik", description="Sunucudaki en yüksek XP'ye sahip ilk 10 üyeyi gösterir.")
async def liderlik(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return

    on_ek = f"{interaction.guild.id}:"
    sirali = sorted(
        ((k, v) for k, v in _veri["xp"].items() if k.startswith(on_ek)),
        key=lambda kv: kv[1],
        reverse=True,
    )[:10]

    if not sirali:
        await interaction.response.send_message("Henüz hiç XP verisi yok.", ephemeral=True)
        return

    satirlar = []
    for i, (anahtar, xp) in enumerate(sirali, 1):
        user_id = int(anahtar.split(":")[1])
        uye = interaction.guild.get_member(user_id)
        ad = uye.display_name if uye else f"<@{user_id}>"
        seviye = _seviye_hesapla(xp)[0]
        satirlar.append(f"**{i}.** {ad} — Seviye {seviye} ({xp} XP)")

    await interaction.response.send_message("🏆 **Liderlik tablosu:**\n" + "\n".join(satirlar))


# ============================================
# ÇEKİLİŞ (GIVEAWAY) SİSTEMİ
# ============================================

# mesaj_id -> asyncio.Task
cekilis_gorevleri: dict[int, asyncio.Task] = {}


class CekilisView(discord.ui.View):
    """Çekiliş mesajındaki 'Katıl' butonu. custom_id mesaj id'si içerir
    (persistent: bot restart etse de çalışır)."""

    def __init__(self, mesaj_id: int):
        super().__init__(timeout=None)
        self.mesaj_id = mesaj_id
        katil_butonu = discord.ui.Button(
            label="Katıl",
            emoji="🎉",
            style=discord.ButtonStyle.success,
            custom_id=f"cekilis_katil:{mesaj_id}",
        )
        katil_butonu.callback = self._katil
        self.add_item(katil_butonu)

    async def _katil(self, interaction: discord.Interaction):
        kayit = _veri["cekilisler"].get(str(self.mesaj_id))
        if kayit is None or kayit.get("bitti"):
            await interaction.response.send_message("Bu çekiliş artık aktif değil.", ephemeral=True)
            return

        katilimcilar = kayit.setdefault("katilimcilar", [])
        uid = str(interaction.user.id)
        if uid in katilimcilar:
            katilimcilar.remove(uid)
            cevap = "🎉 Çekilişten çıktın."
        else:
            katilimcilar.append(uid)
            cevap = "🎉 Çekilişe katıldın!"
        _veri_kaydet()

        # Embed'deki katılımcı sayısını canlı tut
        if isinstance(interaction.channel, discord.TextChannel):
            try:
                mesaj = await interaction.channel.fetch_message(self.mesaj_id)
                if mesaj.embeds:
                    embed = mesaj.embeds[0]
                    for i, field in enumerate(embed.fields):
                        if field.name == "Katılımcılar":
                            embed.set_field_at(i, name="Katılımcılar", value=str(len(katilimcilar)))
                            break
                    await mesaj.edit(embed=embed)
            except discord.HTTPException:
                pass

        await interaction.response.send_message(cevap, ephemeral=True)


async def _cekilis_sayaci(mesaj_id: int):
    """Çekilişin bitiş süresini bekler, sonra kazananı çeker."""
    kayit = _veri["cekilisler"].get(str(mesaj_id))
    if kayit is None:
        return
    bekle = kayit["bitis"] - time.time()
    if bekle > 0:
        await asyncio.sleep(bekle)
    await _cekilis_bitir(mesaj_id)


async def _cekilis_bitir(mesaj_id: int):
    kayit = _veri["cekilisler"].get(str(mesaj_id))
    if kayit is None or kayit.get("bitti"):
        return
    kayit["bitti"] = True
    _veri_kaydet()

    kanal = bot.get_channel(kayit["kanal_id"])
    if kanal is None or not isinstance(kanal, discord.TextChannel):
        return

    katilimcilar = [int(u) for u in kayit.get("katilimcilar", [])]

    # Mesajı bul, embed'i "bitti" durumuna getir, butonları kaldır
    try:
        mesaj = await kanal.fetch_message(mesaj_id)
        if mesaj.embeds:
            embed = mesaj.embeds[0]
            embed.title = f"🎉 Çekiliş Bitti: {kayit['odul']}"
            embed.add_field(name="Bitiş", value="Tamamlandı ✅", inline=True)
            for i, field in enumerate(embed.fields):
                if field.name == "Bitiş":
                    embed.set_field_at(i, name="Bitiş", value="Tamamlandı ✅")
                    break
            await mesaj.edit(embed=embed, view=None)
    except discord.HTTPException:
        pass

    if not katilimcilar:
        try:
            await kanal.send(f"🎉 **{kayit['odul']}** çekilişi bitti ama hiç katılımcı olmadı!")
        except discord.HTTPException:
            pass
        return

    kazananlar = random.sample(katilimcilar, min(kayit["kazanan_sayisi"], len(katilimcilar)))
    kazanan_mention = ", ".join(f"<@{k}>" for k in kazananlar)
    try:
        await kanal.send(f"🎉 **{kayit['odul']}** çekilişini kazandın! Tebrikler {kazanan_mention}!")
    except discord.HTTPException:
        pass


@bot.tree.command(name="cekilis", description="Bir çekiliş başlatır (embed + katıl butonu).")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(
    odul="Çekiliş ödülü",
    sure_dk="Çekiliş süresi (dakika)",
    kazanan_sayisi="Kazanan sayısı (varsayılan 1)",
)
async def cekilis(interaction: discord.Interaction, odul: str, sure_dk: int, kazanan_sayisi: int = 1):
    if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("Bu komut sadece bir metin kanalında kullanılabilir.", ephemeral=True)
        return

    if sure_dk < 1:
        sure_dk = 1
    if kazanan_sayisi < 1:
        kazanan_sayisi = 1

    bitis = time.time() + sure_dk * 60

    embed = discord.Embed(
        title=f"🎉 {odul}",
        description=(
            f"Çekiliş başladı! Katılmak için aşağıdaki **🎉 Katıl** butonuna bas.\n"
            f"Çekilişi başlatan: {interaction.user.mention}"
        ),
        color=discord.Color.gold(),
    )
    embed.add_field(name="Bitiş", value=f"<t:{int(bitis)}:R>", inline=True)
    embed.add_field(name="Kazanan Sayısı", value=str(kazanan_sayisi), inline=True)
    embed.add_field(name="Katılımcılar", value="0", inline=True)

    mesaj = await interaction.channel.send(embed=embed)
    kayit = {
        "odul": odul,
        "bitis": bitis,
        "kazanan_sayisi": kazanan_sayisi,
        "katilimcilar": [],
        "kanal_id": interaction.channel.id,
        "bitti": False,
        "baslatan": str(interaction.user),
    }
    _veri["cekilisler"][str(mesaj.id)] = kayit
    _veri_kaydet()

    await mesaj.edit(view=CekilisView(mesaj.id))
    cekilis_gorevleri[mesaj.id] = bot.loop.create_task(_cekilis_sayaci(mesaj.id))

    await interaction.response.send_message("✅ Çekiliş başlatıldı!", ephemeral=True)


# ============================================
# SUNUCU KURULUM SİHİRBAZI (/sunucukur)
# ============================================
# Kanal şablonu: dropdown'larla mevcut kanalların hangilerinin kalacağı seçtirilir,
# seçilmeyen kanallar silinir, şablondaki eksik kanallar kurulur (ticket hariç).
KANAL_SABLONU = {
    "SOHBET": [
        ("kurallar", "metin"),
        ("duyurular", "metin"),
        ("genel-sohbet", "metin"),
        ("oyun-sohbet", "metin"),
        ("medya", "metin"),
        ("bot-komutlari", "metin"),
        ("cekilisler", "metin"),
        ("yardim", "metin"),
    ],
    "SES": [
        ("genel", "ses"),
        ("oyun", "ses"),
        ("muzik", "ses"),
    ],
    "YONETIM": [
        ("yonetim", "metin"),
    ],
}

# YÖNETİM kanalını görebilecek rol adları (/rolayarla ile kurulanlar)
MODERATOR_ROL_ADLARI = ["Kurucu", "Yönetici", "Moderatör", "Yardımcı"]


# ============================================
# WEB PANELİ (müzik kontrolü)
# ============================================
# Discord OAuth2 ile giriş yapılır; kullanıcının sunucuda MODERATOR_ROL_ADLARI
# rollerinden birine sahip olması gerekir. Aynı process içinde aiohttp ile
# çalışır (ayrı servis gerekmez). Erişim: PUBLIK_URL adresinden.
WEB_PORT = int(os.getenv("PORT", os.getenv("WEB_PORT", "8080")))
PUBLIK_URL = os.getenv("PUBLIK_URL", "http://localhost:8080")
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
WEB_SESSION_SANIYE = 60 * 60 * 12  # 12 saat

# basit oturum deposu: token -> user_id
_web_oturumlar: dict[str, int] = {}
_web_oturum_tarih: dict[str, float] = {}
_web_secili_sunucu: dict[str, int] = {}  # token -> seçili guild_id (panelde sunucu değiştirme)


class OnayView(discord.ui.View):
    """İki butonlu basit onay ekranı (Evet / Vazgeç)."""

    def __init__(self, onayla, vazgec, etiket: str = "✅ Onayla"):
        super().__init__(timeout=120)
        evet = discord.ui.Button(label=etiket, style=discord.ButtonStyle.success, row=0)
        evet.callback = onayla
        hayir = discord.ui.Button(label="❌ Vazgeç", style=discord.ButtonStyle.secondary, row=0)
        hayir.callback = vazgec
        self.add_item(evet)
        self.add_item(hayir)


class SunucuKurView(discord.ui.View):
    """Mevcut kanalları + şablon eksiklerini dropdown'larla seçtirir; seçilmeyenleri siler."""

    SAYFA_BASINA = 2  # her sayfada en fazla 2 dropdown (her biri 2 satır kaplar, toplam 5 satır var)

    def __init__(self, embed: discord.Embed, gruplar: list[tuple[str, list[discord.SelectOption]]]):
        super().__init__(timeout=600)
        self.embed = embed
        self.gruplar = gruplar  # orijinal gruplar (vazgeç/tekrar çiz için)
        self.sayfa = 0
        self.secimler: dict[int, bool] = {}   # kanal_id -> kalacak mı
        self.sablon_secimler: dict[str, bool] = {}  # şablon adı -> kurulacak mı

        # 25'ten büyük grupları 25'erlik parçalara böl (select başına max 25 seçenek)
        self.birimler: list[tuple[str, list[discord.SelectOption]]] = []
        for etiket, secenekler in gruplar:
            for parca in range(0, len(secenekler), 25):
                dilim = secenekler[parca:parca + 25]
                parca_etiket = etiket if len(secenekler) <= 25 else f"{etiket} ({parca // 25 + 1})"
                self.birimler.append((parca_etiket, dilim))

        # Başlangıçta tümü seçili varsay
        for etiket, secenekler in self.birimler:
            for secenek in secenekler:
                if secenek.value.startswith("sablon:"):
                    self.sablon_secimler[secenek.value.split(":", 1)[1]] = True
                else:
                    self.secimler[int(secenek.value)] = True

        self._sablon_ciz()

    def _sablon_ciz(self):
        """Mevcut sayfadaki dropdown'ları + gezinme/kur butonlarını çizer."""
        self.clear_items()
        birim_sayisi = len(self.birimler)
        sayfa_sayisi = max(1, (birim_sayisi + self.SAYFA_BASINA - 1) // self.SAYFA_BASINA)
        if self.sayfa >= sayfa_sayisi:
            self.sayfa = sayfa_sayisi - 1
        bas = self.sayfa * self.SAYFA_BASINA

        for yer, asil_i in enumerate(range(bas, min(bas + self.SAYFA_BASINA, birim_sayisi))):
            etiket, secenekler = self.birimler[asil_i]
            secili = 0
            for secenek in secenekler:
                if secenek.value.startswith("sablon:"):
                    ad = secenek.value.split(":", 1)[1]
                    secenek.default = self.sablon_secimler.get(ad, False)
                    if self.sablon_secimler.get(ad):
                        secili += 1
                else:
                    kid = int(secenek.value)
                    secenek.default = self.secimler.get(kid, False)
                    if self.secimler.get(kid):
                        secili += 1

            select = discord.ui.Select(
                custom_id=f"sukur_birim_{asil_i}",
                placeholder=f"{etiket} ({secili}/{len(secenekler)} seçili)",
                options=secenekler,
                min_values=0,
                max_values=len(secenekler),
                row=yer * 2,  # 0 veya 2 (select 2 satır kaplar)
            )
            select.callback = lambda inter, s=select: self._secim(inter, s)
            self.add_item(select)

        # Gezinme + Kur butonları (satır 4)
        if sayfa_sayisi > 1:
            once = discord.ui.Button(label="◀", style=discord.ButtonStyle.secondary, row=4, disabled=self.sayfa == 0)
            once.callback = self._onceki
            self.add_item(once)
            sayfa_btn = discord.ui.Button(
                label=f"{self.sayfa + 1}/{sayfa_sayisi}", style=discord.ButtonStyle.secondary, row=4, disabled=True
            )
            self.add_item(sayfa_btn)
            sonra = discord.ui.Button(
                label="▶", style=discord.ButtonStyle.secondary, row=4, disabled=self.sayfa >= sayfa_sayisi - 1
            )
            sonra.callback = self._sonraki
            self.add_item(sonra)

        kur = discord.ui.Button(label="⚙️ Onayla ve Uygula", style=discord.ButtonStyle.danger, row=4)
        kur.callback = self._kur_onay
        self.add_item(kur)

    async def _onceki(self, interaction: discord.Interaction):
        self.sayfa = max(0, self.sayfa - 1)
        self._sablon_ciz()
        await interaction.response.edit_message(view=self)

    async def _sonraki(self, interaction: discord.Interaction):
        sayfa_sayisi = max(1, (len(self.birimler) + self.SAYFA_BASINA - 1) // self.SAYFA_BASINA)
        self.sayfa = min(sayfa_sayisi - 1, self.sayfa + 1)
        self._sablon_ciz()
        await interaction.response.edit_message(view=self)

    async def _secim(self, interaction: discord.Interaction, select: discord.ui.Select):
        secilen_set = set(select.values)
        for secenek in select.options:
            if secenek.value.startswith("sablon:"):
                ad = secenek.value.split(":", 1)[1]
                self.sablon_secimler[ad] = secenek.value in secilen_set
            else:
                kanal_id = int(secenek.value)
                self.secimler[kanal_id] = secenek.value in secilen_set
        self._sablon_ciz()
        await interaction.response.edit_message(view=self)

    async def _kur_onay(self, interaction: discord.Interaction):
        guild = interaction.guild
        kalacak = {kid for kid, sec in self.secimler.items() if sec}
        kurulacak = {ad for ad, sec in self.sablon_secimler.items() if sec}

        silinecek = 0
        for kanal in guild.channels:
            if isinstance(kanal, discord.CategoryChannel):
                continue
            if kanal.name.startswith(TICKET_KANAL_ON_EK):
                continue
            if kanal.id not in kalacak:
                silinecek += 1

        embed = discord.Embed(
            title="⚠️ Onay Gerekli",
            description=(
                f"**{len(kalacak)} kanal** korunacak, **{silinecek} kanal** silinecek, "
                f"**{len(kurulacak)} şablon kanalı** kurulacak.\n\n"
                f"Ticket kanalları (ticket-*) korunur. Onaylıyor musun?"
            ),
            color=discord.Color.orange(),
        )
        onay = OnayView(
            onayla=self._kur_uygula,
            vazgec=self._vazgec,
            etiket="✅ Onayla ve Uygula",
        )
        await interaction.response.edit_message(embed=embed, view=onay)

    async def _vazgec(self, interaction: discord.Interaction):
        await interaction.response.edit_message(embed=self.embed, view=SunucuKurView(self.embed, self.gruplar))

    async def _kur_uygula(self, interaction: discord.Interaction):
        await interaction.response.defer()
        kalacak = {kid for kid, sec in self.secimler.items() if sec}
        kurulacak = {ad for ad, sec in self.sablon_secimler.items() if sec}
        sonuc = await _sunucu_kur(interaction.guild, kalacak, kurulacak)
        await interaction.followup.edit_message(interaction.message.id, content=sonuc, embed=None, view=None)


async def _sunucu_kur(guild: discord.Guild, kalacak: set[int], kurulacak: set[str]) -> str:
    """Seçilmeyen kanalları siler, eksik şablon kanallarını kurar, YÖNETİM izinlerini uygular."""
    kurulan = 0
    silinen = 0
    mod_rolleri = [r for r in guild.roles if r.name in MODERATOR_ROL_ADLARI]

    # 1) Seçilmeyen kanalları sil (ticket korunur)
    for kanal in list(guild.channels):
        if isinstance(kanal, discord.CategoryChannel):
            continue
        if kanal.name.startswith(TICKET_KANAL_ON_EK):
            continue
        if kanal.id in kalacak:
            continue
        try:
            await kanal.delete(reason="/sunucukur - seçim temizliği")
            silinen += 1
        except discord.HTTPException:
            pass

    # 2) Boş kalan kategorileri sil (Tickets hariç)
    for kategori in list(guild.categories):
        if kategori.name == TICKET_KATEGORI_ADI:
            continue
        if not kategori.channels:
            try:
                await kategori.delete(reason="/sunucukur - boş kategori")
                silinen += 1
            except discord.HTTPException:
                pass

    # 3) Eksik şablon kanallarını kur
    for kategori_adi, kanallar in KANAL_SABLONU.items():
        kategori = discord.utils.get(guild.categories, name=kategori_adi)
        if kategori is None:
            try:
                kategori = await guild.create_category(kategori_adi, reason="/sunucukur - şablon")
            except discord.HTTPException:
                kategori = None

        for kanal_adi, tur in kanallar:
            if kanal_adi not in kurulacak:
                continue
            if discord.utils.get(guild.channels, name=kanal_adi) is not None:
                continue

            overwrites = None
            # kurallar / duyurular sadece okunabilir olsun (yazmak için yetkili rol gerek)
            if kanal_adi in ("kurallar", "duyurular"):
                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(
                        view_channel=True, read_message_history=True, send_messages=False
                    ),
                    guild.me: discord.PermissionOverwrite(send_messages=True, read_message_history=True),
                }
                for rol in mod_rolleri:
                    overwrites[rol] = discord.PermissionOverwrite(send_messages=True, read_message_history=True)

            try:
                if tur == "ses":
                    await guild.create_voice_channel(kanal_adi, category=kategori, reason="/sunucukur")
                else:
                    await guild.create_text_channel(
                        kanal_adi, category=kategori, overwrites=overwrites, reason="/sunucukur"
                    )
                kurulan += 1
            except discord.HTTPException:
                pass

    # 4) YÖNETİM kanalını sadece yetkili rollerine aç (@everyone kapalı)
    yonetim = discord.utils.get(guild.categories, name="YONETIM")
    if yonetim is not None:
        yonetim_izinleri = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            ),
        }
        for rol in mod_rolleri:
            yonetim_izinleri[rol] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            )
        try:
            await yonetim.edit(overwrites=yonetim_izinleri)
        except discord.HTTPException:
            pass

    return f"✅ **Sunucu kurulumu tamamlandı:** {kurulan} şablon kanalı kuruldu, {silinen} kanal silindi."


@bot.tree.command(name="sunucukur", description="Mevcut kanalları seçtirir, şablon eksiklerini kurar (seçilmeyen silinir!).")
@app_commands.checks.has_permissions(administrator=True)
async def sunucukur(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return

    guild = interaction.guild
    gruplar: list[tuple[str, list[discord.SelectOption]]] = []

    def _secenek(kanal):
        etiket = f"🔊 {kanal.name}" if isinstance(kanal, (discord.VoiceChannel, discord.StageChannel)) else f"#{kanal.name}"
        return discord.SelectOption(label=etiket, value=str(kanal.id), default=True)

    # Her mevcut kategori için (ticket hariç)
    for kategori in guild.categories:
        if kategori.name == TICKET_KATEGORI_ADI:
            continue
        kanallar = [
            k for k in kategori.channels
            if isinstance(k, (discord.TextChannel, discord.VoiceChannel, discord.StageChannel))
        ]
        if not kanallar:
            continue
        gruplar.append((kategori.name, [_secenek(k) for k in kanallar]))

    # Kategorisiz kanallar (ticket hariç)
    kategorisiz = [
        k for k in guild.channels
        if isinstance(k, (discord.TextChannel, discord.VoiceChannel, discord.StageChannel))
        and k.category is None
        and not k.name.startswith(TICKET_KANAL_ON_EK)
    ]
    if kategorisiz:
        gruplar.append(("Kategorisiz", [_secenek(k) for k in kategorisiz]))

    # Şablondan eksik kanallar (yeni kurulacaklar)
    sablon_eksik = [
        (ad, tur)
        for kategori_adi, kanallar in KANAL_SABLONU.items()
        for ad, tur in kanallar
        if discord.utils.get(guild.channels, name=ad) is None
    ]
    if sablon_eksik:
        sablon_secenekler = [
            discord.SelectOption(
                label=f"🔊 {ad}" if tur == "ses" else f"#{ad}",
                value=f"sablon:{ad}",
                default=True,
            )
            for ad, tur in sablon_eksik
        ]
        gruplar.append(("Şablon (Yeni kurulacak)", sablon_secenekler))

    embed = discord.Embed(
        title="🏗️ Sunucu Kurulum Sihirbazı",
        description=(
            "Kategorilerdeki kanallardan **kalacakları** seç (hepsi varsayılan seçili).\n"
            "**Şablon (Yeni kurulacak)** bölümünden kurulacak şablon kanallarını seç.\n\n"
            "⚠️ Seçmediğin kanallar **silinir** (ticket-* korunur)."
        ),
        color=discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=embed, view=SunucuKurView(embed, gruplar))


# ============================================
# ROL KURULUM SİHİRBAZI (/rolayarla)
# ============================================
ROL_SABLONU = [
    {
        "anahtar": "kurucu",
        "varsayilan": "Kurucu",
        "renk": 0xE74C3C,
        "aciklama": "Tam yetki (administrator)",
        "izinler": discord.Permissions(administrator=True),
    },
    {
        "anahtar": "yonetici",
        "varsayilan": "Yönetici",
        "renk": 0xE74C3C,
        "aciklama": "Tam yetki (administrator)",
        "izinler": discord.Permissions(administrator=True),
    },
    {
        "anahtar": "moderator",
        "varsayilan": "Moderatör",
        "renk": 0xE67E22,
        "aciklama": "Kick/ban/mesaj ve ses moderasyonu",
        "izinler": discord.Permissions(
            kick_members=True,
            ban_members=True,
            manage_messages=True,
            mute_members=True,
            deafen_members=True,
            move_members=True,
            manage_nicknames=True,
            moderate_members=True,
        ),
    },
    {
        "anahtar": "yardimci",
        "varsayilan": "Yardımcı",
        "renk": 0x3498DB,
        "aciklama": "Mesaj/ses moderasyonu (ban/kick yok)",
        "izinler": discord.Permissions(
            manage_messages=True,
            mute_members=True,
            deafen_members=True,
            move_members=True,
            manage_nicknames=True,
            moderate_members=True,
        ),
    },
]


class RolIsimModal(discord.ui.Modal, title="Rol Adını Değiştir"):
    def __init__(self, anahtar: str, varsayilan_isim: str, kaydet):
        super().__init__()
        self.anahtar = anahtar
        self.kaydet = kaydet
        self.isim_giris = discord.ui.TextInput(
            label="Yeni rol adı",
            default=varsayilan_isim,
            max_length=100,
        )
        self.add_item(self.isim_giris)

    async def on_submit(self, interaction: discord.Interaction):
        await self.kaydet(interaction, self.anahtar, self.isim_giris.value.strip())


class RolAyarlaView(discord.ui.View):
    """Rol şablonu anketi: her rol için aç/kapat + isim değiştir + Kur butonu."""

    def __init__(self, embed: discord.Embed):
        super().__init__(timeout=600)
        self.embed = embed
        self.secimler = {r["anahtar"]: True for r in ROL_SABLONU}
        self.isimler: dict[str, str] = {}
        self.butonlar: dict[str, discord.ui.Button] = {}
        self.isim_butonlari: dict[str, discord.ui.Button] = {}

        for satir, sablon in enumerate(ROL_SABLONU):
            anahtar = sablon["anahtar"]
            secim_buton = discord.ui.Button(
                label=f"✅ {sablon['varsayilan']}",
                style=discord.ButtonStyle.success,
                row=satir,
            )
            secim_buton.callback = lambda inter, a=anahtar: self._secim(inter, a)
            self.butonlar[anahtar] = secim_buton
            self.add_item(secim_buton)

            isim_buton = discord.ui.Button(
                label=f"✏️ {sablon['varsayilan']}",
                style=discord.ButtonStyle.secondary,
                row=satir,
            )
            isim_buton.callback = lambda inter, a=anahtar: self._isim_degistir(inter, a)
            self.isim_butonlari[anahtar] = isim_buton
            self.add_item(isim_buton)

        kur = discord.ui.Button(label="⚙️ Kur", style=discord.ButtonStyle.danger, row=4)
        kur.callback = self._kur_onay
        self.add_item(kur)

    def _gorunen_isim(self, anahtar: str) -> str:
        sablon = next(r for r in ROL_SABLONU if r["anahtar"] == anahtar)
        return self.isimler.get(anahtar, sablon["varsayilan"])

    async def _secim(self, interaction: discord.Interaction, anahtar: str):
        self.secimler[anahtar] = not self.secimler[anahtar]
        buton = self.butonlar[anahtar]
        if self.secimler[anahtar]:
            buton.label = f"✅ {self._gorunen_isim(anahtar)}"
            buton.style = discord.ButtonStyle.success
        else:
            buton.label = f"⬜ {self._gorunen_isim(anahtar)}"
            buton.style = discord.ButtonStyle.secondary
        await interaction.response.edit_message(view=self)

    async def _isim_degistir(self, interaction: discord.Interaction, anahtar: str):
        await interaction.response.send_modal(RolIsimModal(anahtar, self._gorunen_isim(anahtar), self._isim_kaydet))

    async def _isim_kaydet(self, interaction: discord.Interaction, anahtar: str, yeni_isim: str):
        if not yeni_isim:
            await interaction.response.send_message("İsim boş olamaz.", ephemeral=True)
            return
        self.isimler[anahtar] = yeni_isim
        sablon = next(r for r in ROL_SABLONU if r["anahtar"] == anahtar)
        # Görünür isimde üstü çizili değil; kullanıcıya onay mesajı + buton label güncelle
        self.butonlar[anahtar].label = f"✅ {yeni_isim}"
        self.isim_butonlari[anahtar].label = f"✏️ {yeni_isim}"
        await interaction.response.edit_message(view=self)

    async def _kur_onay(self, interaction: discord.Interaction):
        secilen = [r["anahtar"] for r in ROL_SABLONU if self.secimler.get(r["anahtar"])]
        mevcut_rol_sayisi = sum(
            1 for r in interaction.guild.roles if not r.is_default() and r.id not in {mr.id for mr in interaction.guild.me.roles}
        )
        embed = discord.Embed(
            title="⚠️ Onay Gerekli",
            description=(
                f"**{len(secilen)} rol** kurulacak.\n"
                f"**{mevcut_rol_sayisi} mevcut rol** silinecek (bot rollerine dokunulmaz). "
                f"Üyelerin rolleri gidebilir!\n\nOnaylıyor musun?"
            ),
            color=discord.Color.orange(),
        )
        onay = OnayView(
            onayla=self._kur_uygula,
            vazgec=self._vazgec,
            etiket="✅ Onayla ve Rolleri Kur",
        )
        await interaction.response.edit_message(embed=embed, view=onay)

    async def _vazgec(self, interaction: discord.Interaction):
        await interaction.response.edit_message(embed=self.embed, view=RolAyarlaView(self.embed))

    async def _kur_uygula(self, interaction: discord.Interaction):
        await interaction.response.defer()
        secilen = {r["anahtar"] for r in ROL_SABLONU if self.secimler.get(r["anahtar"])}
        kurulan, silinen = await _rolleri_kur(interaction.guild, secilen, self.isimler)
        sonuc = f"✅ **Rol kurulumu tamamlandı:** {kurulan} rol kuruldu, {silinen} rol silindi."
        await interaction.followup.edit_message(interaction.message.id, content=sonuc, embed=None, view=None)


async def _rolleri_kur(guild: discord.Guild, secilen: set[str], isimler: dict[str, str]) -> tuple[int, int]:
    """Mevcut rolleri (bot rollerinin dışında) siler, seçilen şablon rolleri oluşturur."""
    korunacak_idler = {rol.id for rol in guild.me.roles}
    silinen = 0
    for rol in list(guild.roles):
        if rol.is_default() or rol.id in korunacak_idler:
            continue
        try:
            await rol.delete(reason="/rolayarla - şablon kurulumu")
            silinen += 1
        except discord.HTTPException:
            pass

    kurulan = 0
    for sablon in ROL_SABLONU:
        anahtar = sablon["anahtar"]
        if anahtar not in secilen:
            continue
        isim = isimler.get(anahtar, sablon["varsayilan"])
        try:
            rol = await guild.create_role(
                name=isim,
                color=discord.Color(sablon["renk"]),
                permissions=sablon["izinler"],
                hoist=True,
                reason="/rolayarla - şablon",
            )
            kurulan += 1
            if anahtar == "kurucu" and guild.owner is not None:
                try:
                    await guild.owner.add_roles(rol, reason="kurucu rolü ataması")
                except discord.HTTPException:
                    pass
        except discord.HTTPException:
            pass

    return kurulan, silinen


@bot.tree.command(name="rolayarla", description="Rol şablonunu kurar (mevcut rolleri siler, isim değiştirilebilir!).")
@app_commands.checks.has_permissions(administrator=True)
async def rolayarla(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return

    satirlar = []
    for sablon in ROL_SABLONU:
        satirlar.append(f"- **{sablon['varsayilan']}** — {sablon['aciklama']}")
    embed = discord.Embed(
        title="🎖️ Rol Kurulum Şablonu",
        description=(
            "Kurulacak rolleri seç, istediğin rolün adını ✏️ ile değiştir.\n"
            "**Kur'a bastığında mevcut roller silinir** (bot rollerine dokunulmaz, "
            "sahip 'Kurucu' rolünü otomatik alır).\n\n" + "\n".join(satirlar)
        ),
        color=discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=embed, view=RolAyarlaView(embed))


# ============================================
# TIMEOUT KOMUTLARI (/sustur)
# ============================================

@bot.tree.command(name="sustur", description="Kullanıcıya süreli timeout uygular (otomatik kalkar).")
@app_commands.describe(user="Susturulacak kullanıcı", sure="Süre (dakika, max 40320 = 28 gün)")
@app_commands.checks.has_permissions(moderate_members=True)
async def sustur(interaction: discord.Interaction, user: discord.Member, sure: app_commands.Range[int, 1, 40320]):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return

    if not interaction.guild.me.guild_permissions.moderate_members:
        await interaction.response.send_message("Botun 'Üyeleri Zaman Aşımına Uğrat' iznine ihtiyacım var.", ephemeral=True)
        return
    if user.top_role >= interaction.guild.me.top_role:
        await interaction.response.send_message(
            f"{user.mention} kullanıcısının rolü benimkinden yüksek/eşit, susturamam.", ephemeral=True
        )
        return
    if user.timed_out_until is not None:
        await interaction.response.send_message(f"{user.mention} zaten susturulmuş.", ephemeral=True)
        return

    bitis = discord.utils.utcnow() + datetime.timedelta(minutes=sure)
    try:
        await user.timeout(bitis, reason=f"{interaction.user} tarafından /sustur")
    except discord.HTTPException as e:
        await interaction.response.send_message(f"Uygulanamadı: {e}", ephemeral=True)
        return

    await interaction.response.send_message(
        f"🤐 {user.mention} **{sure} dakika** susturuldu. Bitiş: <t:{int(bitis.timestamp())}:R>"
    )
    await _log_gonder(
        interaction.guild,
        "🤐 Timeout",
        f"{user.mention} **{sure} dakika** susturuldu.\nBitiş: <t:{int(bitis.timestamp())}:R>\nYetkili: {interaction.user.mention}",
        discord.Color.orange(),
    )


@bot.tree.command(name="susturkaldir", description="Kullanıcının timeout'unu kaldırır.")
@app_commands.describe(user="Timeout'u kaldırılacak kullanıcı")
@app_commands.checks.has_permissions(moderate_members=True)
async def susturkaldir(interaction: discord.Interaction, user: discord.Member):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return

    if user.timed_out_until is None:
        await interaction.response.send_message(f"{user.mention} zaten susturulmuş değil.", ephemeral=True)
        return

    try:
        await user.timeout(None, reason=f"{interaction.user} tarafından /susturkaldir")
    except discord.HTTPException as e:
        await interaction.response.send_message(f"Kaldırılamadı: {e}", ephemeral=True)
        return

    await interaction.response.send_message(f"✅ {user.mention} timeout'u kaldırıldı.")


# ============================================
# HATIRLATICI / ANKET / RAPOR / SAYAÇ / PING
# ============================================

@bot.tree.command(name="hatirlat", description="Belirli süre sonra sana DM'den hatırlatma gönderir.")
@app_commands.describe(dakika="Kaç dakika sonra", mesaj="Hatırlatma mesajı")
async def hatirlat(interaction: discord.Interaction, dakika: int, mesaj: str):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return

    if dakika < 1:
        dakika = 1

    await interaction.response.send_message(
        f"⏰ **{dakika} dakika** sonra hatırlatılacak: *{mesaj}*", ephemeral=True
    )
    await asyncio.sleep(dakika * 60)
    try:
        await interaction.user.send(f"⏰ **Hatırlatma:** {mesaj}")
    except discord.HTTPException:
        pass


class AnketView(discord.ui.View):
    """Butonlu oylama: her seçenek için bir buton, oy verince canlı sonuç güncellenir."""

    def __init__(self, soru: str, secenekler: list[str]):
        super().__init__(timeout=600)
        self.soru = soru
        self.secenekler = secenekler
        self.oylar: dict[int, set[int]] = {i: set() for i in range(len(secenekler))}
        emojiler = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
        for i, sec in enumerate(secenekler):
            buton = discord.ui.Button(
                label=sec, emoji=emojiler[i], style=discord.ButtonStyle.primary, row=i % 5
            )
            buton.callback = lambda inter, i=i: self._oy(inter, i)
            self.add_item(buton)

    async def _oy(self, interaction: discord.Interaction, secim_index: int):
        uid = interaction.user.id
        for i in range(len(self.secenekler)):
            self.oylar[i].discard(uid)
        self.oylar[secim_index].add(uid)
        await self._embed_guncelle(interaction)
        await interaction.response.send_message(
            f"✅ Oyun kaydedildi: **{self.secenekler[secim_index]}**", ephemeral=True
        )

    async def _embed_guncelle(self, interaction: discord.Interaction):
        embed = interaction.message.embeds[0]
        for i, sec in enumerate(self.secenekler):
            embed.set_field_at(i, name=f"{i + 1}. {sec}", value=f"✅ {len(self.oylar[i])} oy", inline=True)
        await interaction.message.edit(embed=embed)


@bot.tree.command(name="anket", description="Butonlu oylama başlatır (seçenekleri virgülle ayır).")
@app_commands.describe(soru="Anket sorusu", secenekler="Seçenekler (virgülle ayır, en fazla 5)")
async def anket(interaction: discord.Interaction, soru: str, secenekler: str):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return

    secenek_listesi = [s.strip() for s in secenekler.split(",") if s.strip()][:5]
    if len(secenek_listesi) < 2:
        await interaction.response.send_message("En az 2 seçenek girmelisin (virgülle ayır).", ephemeral=True)
        return

    embed = discord.Embed(title="📊 Anket", description=soru, color=discord.Color.blurple())
    for i, sec in enumerate(secenek_listesi, start=1):
        embed.add_field(name=f"{i}. {sec}", value="✅ 0 oy", inline=True)
    embed.set_footer(text=f"{interaction.user.display_name} tarafından başlatıldı")
    await interaction.response.send_message(embed=embed, view=AnketView(soru, secenek_listesi))


@bot.tree.command(name="rapor", description="Bir kullanıcıyı yetkili kanalına anonim olarak bildirir.")
@app_commands.describe(user="Bildirilecek kullanıcı", sebep="Şikayet sebebi")
async def rapor(interaction: discord.Interaction, user: discord.Member, sebep: str):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return

    if user == interaction.user:
        await interaction.response.send_message("Kendini bildiremezsin.", ephemeral=True)
        return

    kanal_id = _veri["log_kanali"].get(str(interaction.guild.id))
    if not kanal_id:
        await interaction.response.send_message(
            "Yetkililerin önce `/logkanali` komutuyla bir log kanalı ayarlaması lazım.", ephemeral=True
        )
        return
    kanal = interaction.guild.get_channel(int(kanal_id))
    if kanal is None or not isinstance(kanal, discord.TextChannel):
        await interaction.response.send_message("Log kanalı bulunamadı.", ephemeral=True)
        return

    embed = discord.Embed(
        title="🚨 Yeni Rapor",
        description=f"Hedef: {user.mention}\nSebep: **{sebep}**",
        color=discord.Color.red(),
    )
    embed.set_footer(text="Anonim rapor — bildiren kişi gösterilmez.")
    await kanal.send(embed=embed)
    await interaction.response.send_message("✅ Raporun yetkili ekibe iletildi.", ephemeral=True)


@bot.tree.command(name="sayac", description="Bir ses kanalını üye veya ses sayaç kanalı yapar.")
@app_commands.describe(tur="Sayaç türü: uye veya ses", kanal="Sayaç olarak kullanılacak ses kanalı")
@app_commands.checks.has_permissions(manage_guild=True)
async def sayac(interaction: discord.Interaction, tur: str, kanal: discord.VoiceChannel):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return

    if tur not in ("uye", "ses"):
        await interaction.response.send_message("Geçerli türler: `uye` veya `ses`.", ephemeral=True)
        return

    sayaclar = _veri["sayac"].setdefault(str(interaction.guild.id), {})
    sayaclar[tur] = {"kanal_id": str(kanal.id), "ad": kanal.name}
    _veri_kaydet()
    await _sayac_kanali_guncelle(kanal, tur)

    tur_metni = "üye sayacı" if tur == "uye" else "ses sayacı"
    await interaction.response.send_message(f"✅ **{kanal.name}** artık {tur_metni} (60 sn'de bir güncellenir).")


@bot.tree.command(name="sayackapat", description="Ayarlanmış bir sayaç kanalını kapatır.")
@app_commands.describe(tur="Kapatılacak sayaç türü: uye veya ses")
@app_commands.checks.has_permissions(manage_guild=True)
async def sayackapat(interaction: discord.Interaction, tur: str):
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut sadece sunucuda kullanılabilir.", ephemeral=True)
        return

    if tur not in ("uye", "ses"):
        await interaction.response.send_message("Geçerli türler: `uye` veya `ses`.", ephemeral=True)
        return

    sayaclar = _veri["sayac"].setdefault(str(interaction.guild.id), {})
    if tur not in sayaclar:
        await interaction.response.send_message("Bu türde ayarlanmış bir sayaç yok.", ephemeral=True)
        return

    del sayaclar[tur]
    _veri_kaydet()
    await interaction.response.send_message(f"✅ {tur} sayacı kapatıldı.")


@bot.tree.command(name="ping", description="Bot gecikmesini gösterir.")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"🏓 Pong! Gecikme: **{round(bot.latency * 1000)}ms**")


@bot.tree.command(name="botbilgi", description="Bot hakkında bilgi gösterir.")
async def botbilgi(interaction: discord.Interaction):
    komut_sayisi = len(bot.tree.get_commands())
    calisma_suresi = int(time.time() - BASLANGIC_ZAMANI)
    saat = calisma_suresi // 3600
    dk = (calisma_suresi % 3600) // 60

    embed = discord.Embed(title=f"{bot.user} — Bot Bilgisi", color=discord.Color.blurple())
    embed.add_field(name="🏓 Gecikme", value=f"{round(bot.latency * 1000)}ms", inline=True)
    embed.add_field(name="🖥️ Sunucu sayısı", value=str(len(bot.guilds)), inline=True)
    embed.add_field(name="📜 Komut sayısı", value=str(komut_sayisi), inline=True)
    embed.add_field(name="⏱️ Çalışma süresi", value=f"{saat} saat {dk} dk", inline=True)
    await interaction.response.send_message(embed=embed)


# ============================================
# KOMUT HATA YAKALAYICI
# ============================================

@gezdir.error
@stop.error
@mute.error
@mutesg.error
@muteall.error
@mutedurdur.error
@ceza.error
@isimkilit.error
@isimkilitdurdur.error
@atkovala.error
@atkovaladurdur.error
@godmode.error
@godmodedurdur.error
@sil.error
@kilitle.error
@ticketpanel.error
@play.error
@skip.error
@sira_goster.error
@kuyruktemizle.error
@duraklat.error
@devam.error
@dur.error
@yedi_yirmi_dort_aktif.error
@uyari.error
@uyarilar.error
@uyarisil.error
@kick.error
@ban.error
@unban.error
@logkanali.error
@koruma.error
@cekilis.error
@level.error
@liderlik.error
@sunucukur.error
@rolayarla.error
@sustur.error
@susturkaldir.error
@hatirlat.error
@anket.error
@rapor.error
@sayac.error
@sayackapat.error
@ping.error
@botbilgi.error
async def komut_hata(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "Bu komutu kullanmak için gerekli sunucu yetkisine sahip değilsin.", ephemeral=True
        )
    else:
        if not interaction.response.is_done():
            await interaction.response.send_message(f"Bir hata oluştu: {error}", ephemeral=True)
        print(f"Komut hatası: {error}")


# ============================================
# WEB PANELİ (OAuth2 + API + arayüz)
# ============================================

def _web_cookie_al(request: aiohttp.web.Request) -> int | None:
    """Geçerli oturum varsa kullanıcı id döner, yoksa None."""
    token = request.cookies.get("dvrms")
    if not token:
        return None
    user_id = _web_oturumlar.get(token)
    if user_id is None:
        return None
    if time.time() - _web_oturum_tarih.get(token, 0) > WEB_SESSION_SANIYE:
        _web_oturumlar.pop(token, None)
        _web_oturum_tarih.pop(token, None)
        return None
    return user_id


def _web_kullanici_yetkili(guild: discord.Guild, user_id: int) -> bool:
    """Kullanıcının bu sunucuda moderator rolü / sahiplik / admin izni var mı?"""
    if guild.owner_id == user_id:
        return True
    uye = guild.get_member(user_id)
    if uye is None:
        return False
    if uye.guild_permissions.administrator:
        return True
    return any(r.name in MODERATOR_ROL_ADLARI for r in uye.roles)


# Web panelinde seçilen ses kanalı: guild_id -> kanal_id (bellekte, geçici)
_web_secili_kanal: dict[int, int] = {}


def _web_sunucu_kontrol(guild: discord.Guild) -> discord.VoiceChannel | None:
    """Web paneli için çalınacak ses kanalını bulur (panel seçimi > sabit > ilk uygun)."""
    secili_id = _web_secili_kanal.get(guild.id)
    if secili_id:
        kanal = guild.get_channel(secili_id)
        if isinstance(kanal, discord.VoiceChannel) and kanal.permissions_for(guild.me).connect:
            return kanal
    ayar_id = _veri.get("sabit_kanal", {}).get(str(guild.id))
    if ayar_id:
        kanal = guild.get_channel(int(ayar_id))
        if isinstance(kanal, discord.VoiceChannel) and kanal.permissions_for(guild.me).connect:
            return kanal
    for vc in guild.voice_channels:
        if vc.permissions_for(guild.me).connect:
            return vc
    return None


def _web_durum_json(guild: discord.Guild, user_id: int | None = None) -> dict:
    """Panel için anlık durum (çalan şarkı, kuyruk, ses kanalı)."""
    sira = _sira_al(guild.id)
    ses_client = discord.utils.get(bot.voice_clients, guild=guild)

    simdi = None
    if sira.simdi_calan is not None:
        simdi = {
            "baslik": sira.simdi_calan.baslik,
            "sure": sira.simdi_calan.sure or "",
            "sure_sn": sira.simdi_calan.sure_sn or 0,
            "thumbnail": sira.simdi_calan.thumbnail or "",
            "isteyen": sira.simdi_calan.isteyen.name,
            "sorgu": sira.simdi_calan.sorgu,
            "begenildi": bool(_veri.get("begenilenler", {}).get(f"{guild.id}:{sira.simdi_calan.sorgu}")),
        }

    kuyruk = []
    for kayit in sira.kuyruk[:30]:
        kuyruk.append({
            "baslik": kayit.baslik,
            "sure": kayit.sure or "",
            "isteyen": kayit.isteyen.name,
            "sorgu": kayit.sorgu,
        })

    secili = None
    secili_id = _web_secili_kanal.get(guild.id)
    if secili_id:
        kanal = guild.get_channel(secili_id)
        if isinstance(kanal, discord.VoiceChannel):
            secili = kanal.name

    koruma = _veri.get("koruma", {}).get(str(guild.id), {})
    sayaclar = _veri.get("sayac", {}).get(str(guild.id), {})

    def _kanal_adi_by_id(kanal_id) -> str | None:
        if not kanal_id:
            return None
        kanal = guild.get_channel(int(kanal_id))
        return kanal.name if kanal else None

    uyari_listesi = []
    for kisi_key, uyarilar in _veri.get("uyarilar", {}).items():
        kid, _, uid = kisi_key.partition(":")
        if int(kid) != guild.id:
            continue
        uye = guild.get_member(int(uid))
        uyari_listesi.append({
            "kullanici": uye.name if uye else f"ID:{uid}",
            "adet": len(uyarilar),
        })

    liderlik = []
    xp_verisi = _veri.get("xp", {})
    for anahtar, xp in sorted(xp_verisi.items(), key=lambda kv: kv[1], reverse=True)[:10]:
        kid, _, uid = anahtar.partition(":")
        if int(kid) != guild.id:
            continue
        uye = guild.get_member(int(uid))
        if uye is None or uye.bot:
            continue
        liderlik.append({
            "kullanici": uye.display_name,
            "seviye": _seviye_hesapla(xp)[0],
            "xp": xp,
        })
        if len(liderlik) >= 10:
            break

    return {
        "guild": guild.name,
        "yetkili": user_id is not None and _web_kullanici_yetkili(guild, user_id),
        "dongu": sira.dongu,
        "autoplay": bool(sira.autoplay),
        "ses_kanali": ses_client.channel.name if ses_client and ses_client.is_connected() else None,
        "secili_kanal": secili,
        "caliyor": bool(ses_client and ses_client.is_playing()),
        "duraklatildi": bool(ses_client and ses_client.is_paused()),
        "pozisyon": _sira_pozisyonu(sira),
        "simdi": simdi,
        "kuyruk": kuyruk,
        "kanallar": [
            {"id": str(vc.id), "ad": vc.name, "kisi": len(vc.members)}
            for vc in guild.voice_channels if vc.permissions_for(guild.me).connect
        ],
        "metin_kanallar": [
            {"id": str(tc.id), "ad": tc.name}
            for tc in guild.text_channels if tc.permissions_for(guild.me).send_messages
        ],
        "yönetim": {
            "uye_sayisi": guild.member_count or 0,
            "insan_sayisi": sum(1 for m in guild.members if not m.bot),
            "bot_sayisi": sum(1 for m in guild.members if m.bot),
            "kanal_sayisi": len(guild.channels),
            "metin_kanali": len(guild.text_channels),
            "ses_kanali": len(guild.voice_channels),
            "koruma": {
                oz: bool(koruma.get(oz)) for oz in KORUMA_OZELLIKLERI
            },
            "log_kanali": _kanal_adi_by_id(_veri["log_kanali"].get(str(guild.id))),
            "giris_cikis_kanali": _kanal_adi_by_id(_veri["giris_cikis_kanali"].get(str(guild.id))),
            "sabit_kanal": _kanal_adi_by_id(_veri.get("sabit_kanal", {}).get(str(guild.id))),
            "uye_sayaci": sayaclar.get("uye", {}).get("ad"),
            "ses_sayaci": sayaclar.get("ses", {}).get("ad"),
            "uyarilar": uyari_listesi[:10],
            "liderlik": liderlik,
            "ses_seviyesi": _veri.get("ses_seviyesi", {}).get(str(guild.id), 80),
        },
    }


async def _web_oynat(guild: discord.Guild, sorgu: str, kullanici: discord.Member) -> dict:
    """Web'den şarkı ekler; gerekirse ses kanalına bağlanır."""
    kanal = _web_sunucu_kontrol(guild)
    if kanal is None:
        return {"hata": "Bağlanabilecek bir ses kanalı bulunamadı."}

    ses_client = discord.utils.get(bot.voice_clients, guild=guild)
    try:
        if ses_client is None or not ses_client.is_connected():
            ses_client = await kanal.connect(self_mute=False, self_deaf=True)
        elif ses_client.channel.id != kanal.id:
            await ses_client.move_to(kanal)
    except (discord.ClientException, discord.HTTPException, OSError, asyncio.TimeoutError) as e:
        return {"hata": f"Kanala bağlanılamadı: {e}"}

    try:
        loop = asyncio.get_running_loop()
        bilgi = await loop.run_in_executor(None, functools.partial(_sarki_ara, sorgu))
    except Exception as e:
        return {"hata": _yt_hata_cevir(e)}

    baslik = bilgi.get("title", sorgu)
    sorgu_url = bilgi.get("webpage_url", sorgu)
    kayit = SarkiKaydi(
        sorgu=sorgu_url,
        baslik=baslik,
        isteyen=kullanici,
        kanal=guild.system_channel,
        thumbnail=bilgi.get("thumbnail"),
        sure=_sure_metni(bilgi.get("duration")),
        sure_sn=bilgi.get("duration"),
    )

    sira = _sira_al(guild.id)
    sira.kuyruk.append(kayit)

    if sira.simdi_calan is None and not ses_client.is_playing() and not ses_client.is_paused():
        await _sonrakini_cal(guild)

    return {"ok": True, "baslik": baslik}


def _web_oturum_kur(user_id: int) -> str:
    """Yeni oturum token'ı oluşturur ve döndürür."""
    token = os.urandom(24).hex()
    _web_oturumlar[token] = user_id
    _web_oturum_tarih[token] = time.time()
    return token


async def _web_oauth_token_al(kod: str) -> dict | None:
    """Discord OAuth2 authorization code'unu access token'a çevirir."""
    veri = {
        "client_id": DISCORD_CLIENT_ID,
        "client_secret": DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": kod,
        "redirect_uri": f"{PUBLIK_URL}/callback",
    }
    async with aiohttp.ClientSession() as oturum:
        async with oturum.post("https://discord.com/api/oauth2/token", data=veri) as yanit:
            if yanit.status != 200:
                return None
            return await yanit.json()


async def _web_oauth_kullanici(token: str) -> dict | None:
    """Access token ile kullanıcı bilgisini çeker."""
    async with aiohttp.ClientSession() as oturum:
        async with oturum.get(
            "https://discord.com/api/users/@me", headers={"Authorization": f"Bearer {token}"}
        ) as yanit:
            if yanit.status != 200:
                return None
            return await yanit.json()


WEB_HTML = """<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DVRM Müzik Paneli</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Space+Grotesk:wght@500;600;700&family=JetBrains+Mono:wght@500&display=swap" rel="stylesheet">
<style>
:root {
  --bg:#04010a; --bg2:#0b0516;
  --accent:#a78bfa; --accent2:#7c3aed; --accent3:#c084fc; --accent4:#6d28d9;
  --text:#f4f5fb; --muted:#a89fc9; --muted2:#756d9c;
  --green:#34d399; --yellow:#fbbf24; --red:#f87171;
  --line:rgba(168,85,247,.16); --line2:rgba(168,85,247,.32);
  --glass:rgba(19,11,40,.5); --glass2:rgba(30,17,60,.6);
  --font-body:'Inter',system-ui,-apple-system,'Segoe UI',sans-serif;
  --font-head:'Space Grotesk','Inter',system-ui,sans-serif;
  --font-mono:'JetBrains Mono',ui-monospace,monospace;
}
* { margin:0; padding:0; box-sizing:border-box; }
html { -webkit-font-smoothing:antialiased; -moz-osx-font-smoothing:grayscale; text-rendering:optimizeLegibility; }
body { font-family:var(--font-body); background:var(--bg); color:var(--text); min-height:100vh; overflow-x:hidden; font-size:15px; line-height:1.55; }

/* ---------- arka plan: galaksi + aurora + yıldızlar ---------- */
body::before { content:''; position:fixed; inset:-20%; z-index:-4; pointer-events:none;
  background: conic-gradient(from 120deg at 50% 30%, #1e1147, #4c1d95, #7c3aed, #a855f7, #3b0764, #1e1147);
  filter: blur(80px) saturate(1.3); opacity:.42;
  animation: aurora-drift 22s ease-in-out infinite alternate; }
@keyframes aurora-drift { 0%{ transform:translate(-3%,-2%) rotate(0deg) scale(1); } 100%{ transform:translate(4%,3%) rotate(10deg) scale(1.1); } }
/* yıldız tarlası: çok katmanlı radial noktalar + titreşim */
body::after { content:''; position:fixed; inset:0; z-index:-3; pointer-events:none; background-repeat:repeat;
  background-size:520px 520px;
  background-image:
    radial-gradient(1.6px 1.6px at 24px 38px, rgba(255,255,255,.9), transparent 100%),
    radial-gradient(1px 1px at 140px 210px, rgba(216,204,255,.8), transparent 100%),
    radial-gradient(1.4px 1.4px at 260px 90px, rgba(255,255,255,.55), transparent 100%),
    radial-gradient(1px 1px at 380px 300px, rgba(196,181,253,.85), transparent 100%),
    radial-gradient(1.2px 1.2px at 450px 180px, rgba(224,214,255,.9), transparent 100%),
    radial-gradient(1px 1px at 60px 450px, rgba(255,255,255,.5), transparent 100%),
    radial-gradient(1.4px 1.4px at 310px 470px, rgba(190,180,255,.8), transparent 100%),
    radial-gradient(1px 1px at 500px 420px, rgba(255,255,255,.65), transparent 100%);
  animation: yildiz-titres 5.5s ease-in-out infinite alternate; }
@keyframes yildiz-titres { 0%{ opacity:.55; transform:scale(1); } 100%{ opacity:1; transform:scale(1.04); } }
/* nebula bulutları */
.blob { position:fixed; border-radius:50%; filter:blur(100px); z-index:-2; pointer-events:none; animation:float 24s ease-in-out infinite; }
.blob.b1 { width:420px; height:420px; background:#7c3aed; top:-130px; left:-100px; opacity:.3; }
.blob.b2 { width:360px; height:360px; background:#a855f7; top:30%; right:-150px; opacity:.14; animation-delay:-8s; }
.blob.b3 { width:330px; height:330px; background:#5b21b6; bottom:-110px; left:18%; opacity:.16; animation-delay:-14s; }
@keyframes float { 0%,100%{ transform:translate(0,0) scale(1); } 50%{ transform:translate(28px,-26px) scale(1.12); } }

::selection { background:rgba(168,85,247,.4); color:#fff; }
button:focus-visible, a:focus-visible, select:focus-visible, input:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }

.wrap { max-width:1080px; margin:0 auto; padding:30px 20px 60px; position:relative; }
.glow { display:none; }

/* ---------- header ---------- */
header { display:flex; align-items:center; justify-content:space-between; margin-bottom:30px; position:relative; z-index:1; padding-bottom:18px; }
header::after { content:''; position:absolute; left:0; right:0; bottom:0; height:1px; background:linear-gradient(90deg,transparent,rgba(168,85,247,.55),rgba(192,132,252,.25),transparent); }
.brand { display:flex; align-items:center; gap:14px; }
.brand .logo { width:48px; height:48px; border-radius:15px; background:linear-gradient(135deg,var(--accent),var(--accent2) 55%,var(--accent3)); display:flex; align-items:center; justify-content:center; color:#fff; box-shadow:0 10px 34px rgba(139,92,246,.5), inset 0 1px 0 rgba(255,255,255,.28), 0 0 60px rgba(139,92,246,.25); position:relative; overflow:hidden; }
.brand .logo::after { content:''; position:absolute; inset:0; background:linear-gradient(120deg,transparent 30%,rgba(255,255,255,.3) 50%,transparent 70%); animation:shine 3.2s ease-in-out infinite; }
@keyframes shine { 0%{ transform:translateX(-100%); } 55%,100%{ transform:translateX(100%); } }
header h1 { font-family:var(--font-head); font-size:22px; letter-spacing:.4px; font-weight:700; line-height:1.15; }
header h1 span { background:linear-gradient(90deg,var(--accent),var(--accent2),var(--accent3)); background-size:200% 100%; -webkit-background-clip:text; -webkit-text-fill-color:transparent; animation:gradshift 6s ease infinite; }
@keyframes gradshift { 0%,100%{ background-position:0% 50%; } 50%{ background-position:100% 50%; } }
header h1 small { display:block; font-family:var(--font-body); font-size:10px; color:var(--muted); font-weight:500; letter-spacing:2.4px; text-transform:uppercase; margin-top:3px; }
.usermenu { display:flex; align-items:center; gap:12px; font-size:14px; color:var(--muted); font-family:var(--font-body); }
.usermenu .sunucu-sel { min-width:150px; max-width:230px; padding:8px 32px 8px 12px; border-radius:11px; font-size:13px; }
.usermenu img { width:38px; height:38px; border-radius:50%; border:2px solid var(--accent); object-fit:cover; box-shadow:0 0 0 4px rgba(168,85,247,.18), 0 0 24px rgba(168,85,247,.3); }

/* ---------- hero istatistik kartları ---------- */
.stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:14px; margin-bottom:20px; position:relative; z-index:1; }
.stat { background:linear-gradient(160deg,rgba(40,24,84,.55),rgba(14,9,34,.45)); border:1px solid var(--line); border-radius:18px; padding:16px 18px; backdrop-filter:blur(18px) saturate(1.3); -webkit-backdrop-filter:blur(18px) saturate(1.3); box-shadow:0 10px 34px rgba(0,0,0,.35), inset 0 1px 0 rgba(255,255,255,.05); position:relative; overflow:hidden; transition:transform .18s, border-color .18s, box-shadow .18s; }
.stat::before { content:''; position:absolute; top:0; left:18px; right:18px; height:2px; border-radius:2px; background:linear-gradient(90deg,var(--accent),var(--accent3)); opacity:.7; box-shadow:0 0 14px rgba(168,85,247,.6); }
.stat:hover { transform:translateY(-3px); border-color:var(--line2); box-shadow:0 14px 44px rgba(0,0,0,.45), 0 0 30px rgba(168,85,247,.15), inset 0 1px 0 rgba(255,255,255,.05); }
.stat .s-ikon { width:34px; height:34px; border-radius:10px; background:rgba(168,85,247,.14); border:1px solid rgba(168,85,247,.25); display:flex; align-items:center; justify-content:center; color:var(--accent); margin-bottom:10px; box-shadow:0 0 18px rgba(168,85,247,.2); }
.stat .s-deger { font-family:var(--font-head); font-size:26px; font-weight:700; letter-spacing:.5px; line-height:1.1; }
.stat .s-etiket { color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:1.2px; margin-top:4px; font-weight:600; }
.stat .s-alt { color:var(--muted2); font-size:12px; margin-top:2px; }
.brand .logo svg { width:26px; height:26px; }
.loginbox .logo svg { width:38px; height:38px; }
.stat .s-ikon svg { width:18px; height:18px; }
.btn svg { width:15px; height:15px; }
.btn-mini svg { width:13px; height:13px; }

/* ---------- butonlar ---------- */
a.btn, button.btn { background:linear-gradient(135deg,var(--accent),var(--accent2) 55%,#6d28d9); color:#fff; border:0; border-radius:12px; padding:11px 20px; font-size:14px; font-weight:600; font-family:var(--font-body); cursor:pointer; text-decoration:none; transition:transform .15s, box-shadow .15s, filter .15s; box-shadow:0 6px 24px rgba(139,92,246,.45), 0 0 40px rgba(139,92,246,.15), inset 0 1px 0 rgba(255,255,255,.22); letter-spacing:.2px; position:relative; overflow:hidden; display:inline-flex; align-items:center; gap:8px; justify-content:center; }
a.btn::after, button.btn::after { content:''; position:absolute; top:0; left:-60%; width:40%; height:100%; background:linear-gradient(100deg,transparent,rgba(255,255,255,.3),transparent); transform:skewX(-20deg); transition:left .5s; }
a.btn:hover::after, button.btn:hover:not(:disabled)::after { left:120%; }
a.btn:hover, button.btn:hover:not(:disabled) { transform:translateY(-2px); box-shadow:0 12px 36px rgba(168,85,247,.55), 0 0 60px rgba(139,92,246,.3), inset 0 1px 0 rgba(255,255,255,.22); }
button.btn:disabled { opacity:.4; cursor:not-allowed; transform:none; box-shadow:none; }
button.ghost { background:var(--glass); color:var(--muted); border:1px solid var(--line); box-shadow:none; }
button.ghost:hover:not(:disabled) { color:var(--text); border-color:var(--line2); box-shadow:0 0 24px rgba(168,85,247,.12); }
.btn-mini { background:rgba(248,113,113,.10); color:var(--red); border:1px solid rgba(248,113,113,.28); border-radius:9px; width:28px; height:28px; font-size:12px; cursor:pointer; flex-shrink:0; transition:all .15s; font-family:var(--font-body); display:inline-flex; align-items:center; justify-content:center; }
.btn-mini:hover { background:rgba(248,113,113,.28); transform:scale(1.1); }

/* ---------- kartlar (liquid glass + gradient çerçeve) ---------- */
.card { background:linear-gradient(165deg,rgba(28,16,60,.55),rgba(8,4,20,.55)); border-radius:22px; padding:22px; margin-bottom:20px; position:relative; z-index:1; backdrop-filter:blur(22px) saturate(1.3); -webkit-backdrop-filter:blur(22px) saturate(1.3); box-shadow:0 16px 50px rgba(0,0,0,.45), inset 0 1px 0 rgba(255,255,255,.05); transition:transform .22s, box-shadow .25s; }
.card::before { content:''; position:absolute; inset:0; border-radius:inherit; padding:1px; pointer-events:none;
  background:linear-gradient(160deg,rgba(168,85,247,.55),rgba(255,255,255,.04) 30%,rgba(192,132,252,.28) 70%,rgba(124,58,237,.5));
  -webkit-mask:linear-gradient(#fff 0 0) content-box,linear-gradient(#fff 0 0);
  -webkit-mask-composite:xor; mask-composite:exclude; }
.card:hover { transform:translateY(-2px); box-shadow:0 18px 60px rgba(0,0,0,.5), 0 0 44px rgba(124,58,237,.1), inset 0 1px 0 rgba(255,255,255,.05); }
.card h2 { font-family:var(--font-head); font-size:13px; margin-bottom:16px; color:var(--muted); letter-spacing:1px; display:flex; align-items:center; gap:8px; font-weight:600; text-transform:uppercase; min-width:0; }
.card h2 svg { color:var(--accent); flex-shrink:0; filter:drop-shadow(0 0 8px rgba(168,85,247,.6)); }
.card h2 .say { margin-left:auto; background:linear-gradient(135deg,var(--accent),var(--accent2)); padding:3px 12px; border-radius:20px; font-size:12px; color:#fff; flex-shrink:0; box-shadow:0 3px 12px rgba(124,58,237,.4); }

/* ---------- şimdi çalıyor ---------- */
.now { display:flex; gap:20px; align-items:center; }
.now .thumb { position:relative; width:118px; height:118px; border-radius:18px; overflow:hidden; flex-shrink:0; background:linear-gradient(145deg,#241a4d,#14102e); box-shadow:0 14px 40px rgba(0,0,0,.5), 0 0 0 1px rgba(168,85,247,.18), 0 0 34px rgba(124,58,237,.22); animation:thumb-glow 3.2s ease-in-out infinite; }
@keyframes thumb-glow { 0%,100%{ box-shadow:0 14px 40px rgba(0,0,0,.5), 0 0 0 1px rgba(168,85,247,.18), 0 0 34px rgba(124,58,237,.22); } 50%{ box-shadow:0 14px 40px rgba(0,0,0,.5), 0 0 0 1px rgba(168,85,247,.5), 0 0 50px rgba(124,58,237,.42); } }
.now .thumb::after { content:''; position:absolute; inset:0; background:linear-gradient(160deg,transparent 55%,rgba(0,0,0,.4)); }
.now .thumb img { width:100%; height:100%; object-fit:cover; }
.now .info { min-width:0; flex:1; }
.now .info h2 { font-family:var(--font-head); font-size:19px; margin-bottom:6px; color:var(--text); display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-weight:600; }
.now .info p { color:var(--muted); font-size:13px; margin-bottom:12px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.badge { display:inline-flex; align-items:center; gap:6px; padding:6px 14px; border-radius:20px; font-size:12px; font-weight:600; font-family:var(--font-head); letter-spacing:.3px; }
.badge.playing { background:rgba(52,211,153,.13); color:var(--green); border:1px solid rgba(52,211,153,.4); box-shadow:0 0 18px rgba(52,211,153,.18); }
.badge.paused { background:rgba(251,191,36,.12); color:var(--yellow); border:1px solid rgba(251,191,36,.4); }
.badge.idle { background:rgba(168,159,201,.12); color:var(--muted); border:1px solid rgba(168,159,201,.35); }
.eq { display:inline-flex; align-items:flex-end; gap:2px; height:14px; margin-left:4px; }
.eq span { width:3px; border-radius:2px; background:var(--green); animation:eq 1s ease-in-out infinite; }
.eq span:nth-child(2){ animation-delay:.2s } .eq span:nth-child(3){ animation-delay:.4s } .eq span:nth-child(4){ animation-delay:.1s }
@keyframes eq { 0%,100%{height:4px} 50%{height:14px} }
.controls { display:flex; gap:10px; margin-top:18px; flex-wrap:wrap; align-items:center; }
.controls .btn svg { width:15px; height:15px; }
.btn-mini:disabled { opacity:.3; cursor:default; transform:none; }
.progres { margin-top:16px; }
.pbar { height:6px; border-radius:6px; background:rgba(168,85,247,.15); overflow:hidden; box-shadow:inset 0 1px 3px rgba(0,0,0,.4); }
.pbar-dolu { height:100%; width:0%; background:linear-gradient(90deg,var(--accent),var(--accent2)); border-radius:6px; box-shadow:0 0 14px rgba(168,85,247,.6); transition:width .4s linear; }
.pbilgi { display:flex; justify-content:space-between; color:var(--muted); font-size:11px; font-family:var(--font-mono); margin-top:6px; }

/* ---------- formlar ---------- */
.sel { -webkit-appearance:none; appearance:none;
  background:rgba(15,8,32,.72) url("data:image/svg+xml;charset=utf8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%23a78bfa' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E") no-repeat right 12px center;
  border:1px solid var(--line2); color:var(--text); border-radius:13px; padding:11px 36px 11px 14px;
  font-size:14px; font-family:var(--font-body); outline:none; min-width:180px; cursor:pointer;
  backdrop-filter:blur(10px); -webkit-backdrop-filter:blur(10px);
  box-shadow:0 4px 18px rgba(0,0,0,.35), inset 0 1px 0 rgba(255,255,255,.05);
  transition:border-color .15s, background .15s, box-shadow .15s, transform .15s; }
.sel:hover { border-color:rgba(168,85,247,.5); box-shadow:0 4px 22px rgba(0,0,0,.4), 0 0 18px rgba(124,58,237,.18); }
.sel:focus { border-color:var(--accent); background-color:rgba(28,15,58,.92); box-shadow:0 0 0 3px rgba(168,85,247,.18), 0 0 28px rgba(124,58,237,.3); }
.sel option { background:#140b2c; color:var(--text); }
.searchbar { display:flex; gap:10px; }
.searchbar input { flex:1; background:rgba(15,8,32,.72); border:1px solid var(--line); border-radius:13px; padding:12px 16px; color:var(--text); font-size:14px; font-family:var(--font-body); outline:none; box-shadow:0 4px 18px rgba(0,0,0,.3), inset 0 1px 0 rgba(255,255,255,.04); transition:border .15s, background .15s, box-shadow .15s; }
.searchbar input:focus { border-color:var(--accent); background:rgba(28,15,58,.85); box-shadow:0 0 0 3px rgba(168,85,247,.18), 0 0 26px rgba(124,58,237,.25); }
.searchbar input::placeholder { color:var(--muted2); }
input[type=range] { flex:1; -webkit-appearance:none; appearance:none; height:6px; border-radius:6px; background:linear-gradient(90deg,var(--accent),var(--accent2),var(--accent3)); outline:none; min-width:0; box-shadow:0 0 14px rgba(168,85,247,.3); }
input[type=range]::-webkit-slider-thumb { -webkit-appearance:none; width:18px; height:18px; border-radius:50%; background:#fff; cursor:pointer; box-shadow:0 2px 10px rgba(0,0,0,.5), 0 0 14px rgba(168,85,247,.7); border:3px solid var(--accent); transition:transform .12s; }
input[type=range]::-webkit-slider-thumb:hover { transform:scale(1.15); }
input[type=range]::-moz-range-thumb { width:18px; height:18px; border-radius:50%; background:#fff; cursor:pointer; border:3px solid var(--accent); }

/* ---------- arama sonuçları ---------- */
.results { margin-top:14px; display:flex; flex-direction:column; gap:8px; max-height:420px; overflow-y:auto; }
.res-item { display:flex; align-items:center; gap:12px; background:var(--glass); padding:10px 12px; border-radius:13px; cursor:pointer; transition:background .15s, transform .1s, border-color .15s; border:1px solid transparent; min-width:0; }
.res-item:hover { background:var(--glass2); transform:translateX(3px); border-color:var(--line2); }
.res-item img { width:52px; height:52px; border-radius:10px; object-fit:cover; flex-shrink:0; box-shadow:0 4px 14px rgba(0,0,0,.4), 0 0 0 1px rgba(168,85,247,.12); }
.res-item .r-body { min-width:0; flex:1; overflow:hidden; }
.res-item .r-title { font-size:13px; font-weight:600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.res-item .r-sub { color:var(--muted); font-size:12px; margin-top:2px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.res-item .r-add { margin-left:auto; color:var(--accent3); font-size:12px; font-weight:700; flex-shrink:0; font-family:var(--font-head); letter-spacing:.3px; text-shadow:0 0 12px rgba(0,229,255,.5); }

/* ---------- sıra listesi ---------- */
.siralist { display:flex; flex-direction:column; gap:6px; }
.siralist .item { display:flex; align-items:center; gap:12px; background:var(--glass); padding:10px 14px; border-radius:11px; font-size:13px; transition:background .15s, border-color .15s; border:1px solid transparent; min-width:0; }
.siralist .item:hover { background:var(--glass2); border-color:var(--line); }
.siralist .n { width:24px; color:var(--muted); text-align:center; font-size:12px; flex-shrink:0; font-family:var(--font-mono); }
.siralist .t { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-weight:500; }
.siralist .s { color:var(--muted); font-size:12px; white-space:nowrap; flex-shrink:0; font-family:var(--font-mono); }
.siralist .head { color:var(--green); font-weight:700; font-family:var(--font-head); }
.empty { color:var(--muted); font-size:14px; text-align:center; padding:22px; font-family:var(--font-body); }
.loading { color:var(--muted); font-size:13px; text-align:center; padding:10px; font-family:var(--font-body); }

/* ---------- login ---------- */
.loginbox { text-align:center; padding:80px 24px; max-width:540px; margin:8vh auto 0; }
.loginbox .logo { width:76px; height:76px; border-radius:22px; background:linear-gradient(135deg,var(--accent),var(--accent2) 55%,var(--accent3)); display:inline-flex; align-items:center; justify-content:center; color:#fff; margin-bottom:22px; box-shadow:0 14px 44px rgba(139,92,246,.5), 0 0 70px rgba(139,92,246,.3); animation:bob 3s ease-in-out infinite; }
@keyframes bob { 0%,100%{ transform:translateY(0); } 50%{ transform:translateY(-9px); } }
.loginbox h2 { font-family:var(--font-head); font-size:28px; margin-bottom:12px; color:var(--text); font-weight:700; }
.loginbox p { color:var(--muted); margin-bottom:34px; line-height:1.7; font-size:14px; }

/* ---------- yönetim ---------- */
.yonet-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); gap:14px; }
.yn-row { display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:14px; margin-top:14px; }
.yn-block { background:var(--glass); border-radius:15px; padding:15px; border:1px solid var(--line); min-width:0; overflow:hidden; transition:border-color .2s; }
.yn-block:hover { border-color:var(--line2); }
.yn-block h3 { font-family:var(--font-head); font-size:11px; color:var(--muted); margin-bottom:11px; text-transform:uppercase; letter-spacing:1.2px; font-weight:600; display:flex; align-items:center; gap:6px; }
.yn-block h3 svg { color:var(--accent); flex-shrink:0; }
.yn-block p { font-size:13px; margin:4px 0; color:var(--text); overflow-wrap:anywhere; }
.yn-block .sel { width:100%; min-width:0; margin-bottom:6px; }
.yn-block .searchbar { flex-wrap:wrap; }
.yn-block .searchbar input { min-width:0; }
.yn-block .searchbar .sel { flex:1 1 100%; }

/* ---------- toggle switch ---------- */
.tgl { position:relative; display:inline-flex; align-items:center; gap:8px; background:var(--glass); border:1px solid var(--line); border-radius:10px; padding:7px 11px; font-size:12px; font-family:var(--font-body); margin:3px 3px 0 0; cursor:pointer; transition:all .18s; user-select:none; }
.tgl:hover { border-color:var(--line2); transform:translateY(-1px); }
.tgl .sw { position:relative; width:30px; height:17px; border-radius:20px; background:rgba(248,113,113,.45); transition:background .2s; flex-shrink:0; }
.tgl .sw::after { content:''; position:absolute; top:2px; left:2px; width:13px; height:13px; border-radius:50%; background:#fff; transition:left .2s; box-shadow:0 1px 4px rgba(0,0,0,.4); }
.tgl.on { background:rgba(167,139,250,.12); border-color:rgba(167,139,250,.45); color:var(--accent); }
.tgl.on .sw { background:var(--accent); box-shadow:0 0 12px rgba(167,139,250,.6); }
.tgl.on .sw::after { left:15px; }
.tgl.off { color:var(--red); }

/* ---------- listeler ---------- */
#uyariListe, #liderlikListe { display:flex; flex-direction:column; gap:4px; overflow-y:auto; }
#uyariListe { max-height:200px; }
#liderlikListe { max-height:280px; }
#uyariListe .item, #liderlikListe .item { display:flex; align-items:center; gap:10px; background:var(--glass); padding:8px 11px; border-radius:9px; font-size:13px; min-width:0; }
#uyariListe .t, #liderlikListe .t { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-weight:500; }
#uyariListe .s, #liderlikListe .s { color:var(--muted); font-size:12px; white-space:nowrap; flex-shrink:0; }
#liderlikListe .item:nth-child(1) { background:linear-gradient(90deg,rgba(251,191,36,.14),transparent 70%); border:1px solid rgba(251,191,36,.4); box-shadow:0 0 18px rgba(251,191,36,.08); }
#liderlikListe .item:nth-child(2) { background:linear-gradient(90deg,rgba(168,159,201,.12),transparent 70%); border:1px solid rgba(168,159,201,.35); }
#liderlikListe .item:nth-child(3) { background:linear-gradient(90deg,rgba(217,119,6,.14),transparent 70%); border:1px solid rgba(217,119,6,.4); }
#liderlikListe .n { width:22px; color:var(--muted); font-family:var(--font-mono); font-size:12px; flex-shrink:0; }

/* ---------- toast ---------- */
#toast { position:fixed; bottom:26px; left:50%; transform:translateX(-50%) translateY(24px); background:rgba(18,12,40,.96); border:1px solid var(--line2); color:var(--text); padding:13px 24px; border-radius:14px; font-size:14px; box-shadow:0 14px 50px rgba(0,0,0,.6), 0 0 30px rgba(124,58,237,.12); opacity:0; pointer-events:none; transition:all .3s; z-index:99; backdrop-filter:blur(14px); -webkit-backdrop-filter:blur(14px); }
#toast.show { opacity:1; transform:translateX(-50%) translateY(0); }
#toast.err { border-color:rgba(248,113,113,.5); box-shadow:0 14px 50px rgba(0,0,0,.6), 0 0 30px rgba(248,113,113,.15); }

::-webkit-scrollbar { width:8px; height:8px; }
::-webkit-scrollbar-thumb { background:rgba(168,85,247,.35); border-radius:8px; }
::-webkit-scrollbar-thumb:hover { background:rgba(168,85,247,.5); }
::-webkit-scrollbar-track { background:transparent; }

/* ---------- karaoke (şarkı sözleri) ---------- */
.soz-kart { margin-top:16px; border-top:1px dashed var(--line2); padding-top:14px; }
.soz-kart.kapali { display:none; }
.soz-bas { display:flex; align-items:center; gap:12px; padding:10px 0; }
.soz-kapak { width:52px; height:52px; border-radius:12px; object-fit:cover; flex-shrink:0; box-shadow:0 6px 20px rgba(0,0,0,.45), 0 0 0 1px rgba(168,85,247,.15); }
.soz-meta { flex:1; min-width:0; }
.soz-ad { font-family:var(--font-head); font-weight:700; font-size:16px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.soz-sanatci { color:var(--muted); font-size:13px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.soz-ikonlar { display:flex; gap:6px; flex-shrink:0; }
.soz-ikon { background:var(--glass); border:1px solid var(--line2); color:var(--text); width:34px; height:34px; border-radius:10px; cursor:pointer; font-size:15px; transition:all .25s; display:flex; align-items:center; justify-content:center; }
.soz-ikon:hover { background:var(--glass2); border-color:var(--line2); transform:translateY(-1px); box-shadow:0 0 16px rgba(168,85,247,.2); }
.soz-ikon.on { color:var(--accent); border-color:rgba(167,139,250,.5); background:rgba(167,139,250,.14); box-shadow:0 0 18px rgba(167,139,250,.3); }
.soz-ikon svg { width:16px; height:16px; }
.soz-liste { height:340px; overflow-y:auto; padding:14px 8px; position:relative; scroll-behavior:smooth; }
.soz-line { filter:blur(4px); opacity:.35; font-weight:600; color:var(--muted); font-size:16px; padding:9px 14px; border-radius:10px; transition:all .4s ease; text-align:center; line-height:1.55; }
.soz-line.near { filter:blur(2px); opacity:.6; color:var(--muted); }
.soz-line.active { filter:blur(0); opacity:1; font-weight:800; color:#fff; text-shadow:0 0 22px rgba(168,85,247,.6); }
.soz-yuk { text-align:center; color:var(--muted); padding:26px 10px; font-size:14px; }

/* ---------- footer ---------- */
footer { margin-top:44px; padding-top:26px; border-top:1px solid var(--line); display:flex; flex-direction:column; align-items:center; gap:14px; text-align:center; position:relative; z-index:1; }
.footer-note { color:var(--muted2); font-size:12px; letter-spacing:.5px; }
.discord-btn { display:inline-flex; align-items:center; gap:10px; background:linear-gradient(135deg,#7c3aed,#4c1d95); color:#fff; text-decoration:none; font-weight:600; font-size:14px; padding:12px 24px; border-radius:14px; font-family:var(--font-head); letter-spacing:.3px; box-shadow:0 8px 30px rgba(124,58,237,.4), 0 0 50px rgba(76,29,149,.25), inset 0 1px 0 rgba(255,255,255,.22); transition:transform .15s, box-shadow .15s; position:relative; overflow:hidden; }
.discord-btn::after { content:''; position:absolute; top:0; left:-60%; width:40%; height:100%; background:linear-gradient(100deg,transparent,rgba(255,255,255,.28),transparent); transform:skewX(-20deg); transition:left .5s; }
.discord-btn:hover::after { left:120%; }
.discord-btn:hover { transform:translateY(-2px); box-shadow:0 12px 40px rgba(124,58,237,.55), 0 0 70px rgba(76,29,149,.35), inset 0 1px 0 rgba(255,255,255,.22); }
.discord-btn svg { width:22px; height:22px; }

/* ---------- responsive ---------- */
@media (max-width:640px) {
  .wrap { padding:20px 14px 50px; }
  .now { flex-direction:column; text-align:center; }
  .now .info { width:100%; }
  .searchbar { flex-wrap:wrap; }
  .searchbar input { flex:1 1 100%; }
  .sel { min-width:0; }
  .controls .sel { flex:1 1 100%; }
  .controls .btn { flex:1 1 40%; justify-content:center; text-align:center; }
  .yn-row { grid-template-columns:1fr; }
  .yonet-grid { grid-template-columns:1fr; }
  .stats { grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); }
}
@media (prefers-reduced-motion: reduce) {
  body::before, body::after, .blob { animation:none; }
  header h1 span { animation:none; }
}
</style>
</head>
<body>
<div class="blob b1"></div>
<div class="blob b2"></div>
<div class="blob b3"></div>
<div id="toast"></div>
<div class="wrap">
<header>
  <div class="brand">
    <div class="logo" id="logoIkon"></div>
    <h1><span>DVRM</span> Paneli<small>Müzik & Sunucu Kontrol</small></h1>
  </div>
  <div class="usermenu" id="usermenu"></div>
</header>

<div id="app"></div>

<footer>
  <a class="discord-btn" href="https://discord.gg/revolutionn" target="_blank" rel="noopener">
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M20.317 4.3698a19.7913 19.7913 0 00-4.8851-1.5152.0741.0741 0 00-.0785.0371c-.211.3753-.4447.8648-.6083 1.2495-1.8447-.2762-3.68-.2762-5.4868 0-.1636-.3933-.4058-.8742-.6177-1.2495a.077.077 0 00-.0785-.037 19.7363 19.7363 0 00-4.8852 1.515.0699.0699 0 00-.0321.0277C.5334 9.0458-.319 13.5799.0992 18.0578a.0824.0824 0 00.0312.0561c2.0528 1.5076 4.0413 2.4228 5.9929 3.0294a.0777.0777 0 00.0842-.0276c.4616-.6304.8731-1.2952 1.226-1.9942a.076.076 0 00-.0416-.1057c-.6528-.2476-1.2743-.5495-1.8722-.8923a.077.077 0 01-.0076-.1277c.1258-.0943.2517-.1923.3718-.2914a.0743.0743 0 01.0776-.0105c3.9278 1.7933 8.18 1.7933 12.0614 0a.0739.0739 0 01.0785.0095c.1202.099.246.1981.3728.2924a.077.077 0 01-.0066.1276 12.2986 12.2986 0 01-1.873.8914.0766.0766 0 00-.0407.1067c.3604.698.7719 1.3628 1.225 1.9932a.076.076 0 00.0842.0286c1.961-.6067 3.9495-1.5219 6.0023-3.0294a.077.077 0 00.0313-.0552c.5004-5.177-.8382-9.6739-3.5485-13.6604a.061.061 0 00-.0312-.0286zM8.02 15.3312c-1.1825 0-2.1569-1.0857-2.1569-2.419 0-1.3332.9555-2.4189 2.157-2.4189 1.2108 0 2.1757 1.0952 2.1568 2.419 0 1.3332-.9555 2.4189-2.1569 2.4189zm7.9748 0c-1.1825 0-2.1569-1.0857-2.1569-2.419 0-1.3332.9554-2.4189 2.1569-2.4189 1.2108 0 2.1757 1.0952 2.1568 2.419 0 1.3332-.946 2.4189-2.1568 2.4189z"/></svg>
    Sunucumuza Katıl
  </a>
  <div class="footer-note">DVRM Müzik Paneli — herkes için müzik, yöneticiler için kontrol</div>
</footer>
</div>

<script>
const IK = {
  muzik: '<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>',
  kulaklik: '<path d="M3 14h3a2 2 0 0 1 2 2v3a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-5Zm18 0h-3a2 2 0 0 0-2 2v3a2 2 0 0 0 2 2h1a2 2 0 0 0 2-2v-5Z"/><path d="M3 14v-3a9 9 0 0 1 18 0v3"/>',
  ara: '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
  liste: '<path d="M8 6h13M8 12h13M8 18h13"/><path d="M3 6h.01M3 12h.01M3 18h.01"/>',
  kullanicilar: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
  kullanici: '<path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
  kupa: '<path d="M6 9H4.5a2.5 2.5 0 0 1 0-5H6"/><path d="M18 9h1.5a2.5 2.5 0 0 0 0-5H18"/><path d="M4 22h16"/><path d="M10 14.66V17c0 .55-.47.98-.97 1.21C7.85 18.75 7 20.24 7 22"/><path d="M14 14.66V17c0 .55.47.98.97 1.21C16.15 18.75 17 20.24 17 22"/><path d="M18 2H6v7a6 6 0 0 0 12 0V2Z"/>',
  ayarlar: '<path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/>',
  kalkan: '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/>',
  grafik: '<path d="M3 3v18h18"/><path d="M18 17V9"/><path d="M13 17V5"/><path d="M8 17v-3"/>',
  yenile: '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/>',
  kare: '<path d="M4 9h16"/><path d="M4 15h16"/><path d="M10 3 8 21"/><path d="M16 3l-2 18"/>',
  kalem: '<path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/>',
  megafon: '<path d="m3 11 18-5v12L3 14v-3z"/><path d="M11.6 16.8a3 3 0 1 1-5.8-1.6"/>',
  ses: '<path d="M11 4.702a.705.705 0 0 0-1.203-.498L6.413 7.587A1.4 1.4 0 0 1 5.416 8H3a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2.416a1.4 1.4 0 0 1 .997.413l3.383 3.384A.705.705 0 0 0 11 19.298z"/><path d="M16 9a5 5 0 0 1 0 6"/><path d="M19.364 18.364a9 9 0 0 0 0-12.728"/>',
  uyari: '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
  mesaj: '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
  bot: '<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 2v2M15 2v2M9 20v2M15 20v2M2 9h2M2 15h2M20 9h2M20 15h2"/>',
  kapi: '<path d="M13 4h3a2 2 0 0 1 2 2v14"/><path d="M2 20h3"/><path d="M13 20h9"/><path d="M10 12v.01"/><path d="M13 4.562v16.157a1 1 0 0 1-1.242.97L5 20V5.562a2 2 0 0 1 1.515-1.94l4-1A2 2 0 0 1 13 4.561Z"/>',
  oynat: '<polygon points="6 3 20 12 6 21 6 3"/>',
  duraklat: '<rect x="14" y="4" width="4" height="16" rx="1"/><rect x="6" y="4" width="4" height="16" rx="1"/>',
  sonraki: '<polygon points="5 4 15 12 5 20 5 4"/><rect x="18" y="5" width="2" height="14"/>',
  durdur: '<rect x="5" y="5" width="14" height="14" rx="2"/>',
  mikrofon: '<path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" x2="12" y1="19" y2="22"/>',
  kalp: '<path d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.3 1.5 4.05 3 5.5l7 7Z"/>',
  noktalar: '<circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/><circle cx="5" cy="12" r="1"/>',
  x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
  kopyala: '<rect width="8" height="4" x="8" y="2" rx="1" ry="1"/><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/>',
  gonder: '<path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/>',
  onay: '<path d="M20 6 9 17l-5-5"/>',
  dongu: '<path d="m17 2 4 4-4 4"/><path d="M3 11v-1a4 4 0 0 1 4-4h14"/><path d="m7 22-4-4 4-4"/><path d="M21 13v1a4 4 0 0 1-4 4H3"/>',
  karistir: '<path d="m18 14 4 4-4 4"/><path d="m18 2 4 4-4 4"/><path d="M2 18h1.973a4 4 0 0 0 3.3-1.7l5.454-8.6a4 4 0 0 1 3.3-1.7H22"/><path d="M2 6h1.972a4 4 0 0 1 3.6 2.2"/><path d="M22 18h-6.041a4 4 0 0 1-3.3-1.8l-.359-.45"/>',
  okYukari: '<path d="m18 15-6-6-6 6"/>',
  okAsagi: '<path d="m6 9 6 6 6-6"/>'
};

function ik(ad, boyut, dolu) {
  const i = IK[ad];
  if (!i) return '';
  const b = boyut || 16;
  return '<svg xmlns="http://www.w3.org/2000/svg" width="' + b + '" height="' + b + '" viewBox="0 0 24 24" fill="' + (dolu ? 'currentColor' : 'none') + '" stroke="' + (dolu ? 'none' : 'currentColor') + '" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + i + '</svg>';
}

const $ = s => document.querySelector(s);
let sonDurum = null;
let sonSonuclar = [];

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function toast(metin, hata) {
  const t = $('#toast');
  if (!t) return;
  t.textContent = metin;
  t.className = hata ? 'show err' : 'show';
  clearTimeout(t._t);
  t._t = setTimeout(() => { t.className = ''; }, 3200);
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (r.status === 401) { location.href = '/login'; throw new Error('401'); }
  const j = await r.json().catch(() => ({}));
  if (j.hata) throw new Error(j.hata);
  return j;
}

function renderLogin() {
  $('#app').innerHTML = `
    <div class="loginbox card">
      <div class="logo">${ik('muzik', 38)}</div>
      <h2>Müzik & Sunucu Kontrol Paneli</h2>
      <p>Şarkı çal, sırayı yönet, korumayı aç ve duyuru gönder — hepsi tek panelden.<br>Botun bulunduğu bir sunucunun üyesi olman yeterli; yönetim araçları yöneticiler için.</p>
      <a class="btn" href="/login">Discord ile giriş yap</a>
    </div>`;
}

function cizPanel(durum) {
  sonDurum = durum;
  const y = durum.yönetim;

  const yonetKart = $('#yonetKart');
  if (yonetKart) yonetKart.style.display = durum.yetkili ? '' : 'none';

  const stSarki = $('#stSarki');
  if (stSarki) {
    stSarki.textContent = durum.simdi ? 'Çalıyor' : (durum.duraklatildi ? 'Duraklatıldı' : '—');
    const stSarkiAlt = $('#stSarkiAlt');
    if (stSarkiAlt) stSarkiAlt.textContent = durum.simdi ? durum.simdi.baslik : 'Sırada şarkı yok';
    if (stSarkiAlt) stSarkiAlt.title = durum.simdi ? durum.simdi.baslik : '';
  }
  const stSira = $('#stSira'); if (stSira) stSira.textContent = durum.kuyruk.length;
  const stUye = $('#stUye'); if (stUye) stUye.textContent = y ? y.uye_sayisi : '—';
  const stUyeAlt = $('#stUyeAlt'); if (stUyeAlt && y) stUyeAlt.textContent = `${y.insan_sayisi} kişi · ${y.bot_sayisi} bot`;
  const stSeviye = $('#stSeviye');
  if (stSeviye) stSeviye.textContent = y && y.liderlik && y.liderlik.length ? 'Sv ' + y.liderlik[0].seviye : '—';
  const stSeviyeAlt = $('#stSeviyeAlt');
  if (stSeviyeAlt) stSeviyeAlt.textContent = y && y.liderlik && y.liderlik.length ? y.liderlik[0].kullanici : 'XP verisi yok';

  let badge = durum.duraklatildi ? '<span class="badge paused">' + ik('duraklat', 14, true) + ' Duraklatıldı</span>'
            : durum.caliyor ? '<span class="badge playing">' + ik('oynat', 14, true) + ' Çalıyor<span class="eq"><span></span><span></span><span></span><span></span></span></span>'
            : '<span class="badge idle">' + ik('durdur', 14, true) + ' Boşta</span>';

  let nowHtml = '<div class="empty">Şu anda çalan şarkı yok.</div>';
  if (durum.simdi) {
    const s = durum.simdi;
    nowHtml = `<div class="now">
      <div class="thumb"><img src="${esc(s.thumbnail)}" onerror="this.style.display='none'"></div>
      <div class="info">
        <h2>${esc(s.baslik)}</h2>
        <p>${esc(s.sure)} · istek: ${esc(s.isteyen)}</p>
        ${badge}
        <button class="btn-mini" onclick="sozAc()" id="btnSoz">${ik('mikrofon', 13)} Sözler</button>
      </div>
    </div>`;
  }

  let queueHtml = '<div class="empty">Sırada şarkı yok.</div>';
  if (durum.kuyruk.length) {
    const son = durum.kuyruk.length - 1;
    queueHtml = `<div class="siralist">` + durum.kuyruk.map((k,i) =>
      `<div class="item"><span class="n">${i+1}</span><span class="t">${esc(k.baslik)}</span>
       <span class="s">${esc(k.sure)} · ${esc(k.isteyen)}</span>
       <button class="btn-mini" onclick="siraTas(${i},${i-1})" title="Yukarı taşı" ${i===0?'disabled':''}>${ik('okYukari', 13)}</button>
       <button class="btn-mini" onclick="siraTas(${i},${i+1})" title="Aşağı taşı" ${i===son?'disabled':''}>${ik('okAsagi', 13)}</button>
       <button class="btn-mini" onclick="siraSil(${i})" title="Sıradan kaldır">${ik('x', 13)}</button></div>`).join('') + `</div>`;
  }

  $('#simdi').innerHTML = nowHtml;
  $('#siraAdet').textContent = durum.kuyruk.length;
  $('#siraListe').innerHTML = queueHtml;

  const btnDongu = $('#btnDongu');
  if (btnDongu) btnDongu.innerHTML = ik('dongu', 15) + ' ' + ['Döngü: Yok','Döngü: Şarkı','Döngü: Kuyruk'][durum.dongu || 0] + (durum.autoplay ? ' · Auto' : '');

  const prog = $('#progresKart');
  if (prog) {
    if (durum.simdi) {
      prog.style.display = '';
      const pToplam = $('#pToplam');
      if (pToplam) pToplam.textContent = (durum.simdi.sure_sn ? formatSure(durum.simdi.sure_sn) : (durum.simdi.sure || '—'));
      ilerlemeBaslat();
    } else {
      prog.style.display = 'none';
      ilerlemeDurdur();
    }
  }

  $('#btnSkip').disabled = !durum.simdi;
  $('#btnPause').disabled = !(durum.caliyor && !durum.duraklatildi);
  $('#btnResume').disabled = !durum.duraklatildi;
  $('#btnStop').disabled = !durum.simdi;

  const aktifKanal = durum.secili_kanal || durum.ses_kanali;
  const chans = (durum.kanallar||[]).map(c =>
    `<option value="${c.id}" ${c.ad === aktifKanal ? 'selected' : ''}>${esc(c.ad)} (${c.kisi})</option>`).join('');
  $('#selChan').innerHTML = chans;

  if (durum.simdi && durum.simdi.sorgu) sozOnbellekYukle(durum.simdi.sorgu);

  cizYonet(durum);
}

function cizYonet(durum) {
  const y = durum.yönetim;
  if (!y) return;

  const koruma = y.koruma || {};
  const korumaMetin = Object.keys(koruma).map(k =>
    `<span class="tgl ${koruma[k] ? 'on' : 'off'}" onclick="yonetKoruma('${k}','${koruma[k]}')"><span class="sw"></span>${esc(k)}</span>`
  ).join(' ');
  const ka = $('#korumaAlan'); if (ka) ka.innerHTML = korumaMetin || '<span class="empty">—</span>';

  const sa = $('#sunucuAlan');
  if (sa) sa.innerHTML = `${ik('kullanicilar', 14)} ${y.insan_sayisi} kişi · ${ik('bot', 14)} ${y.bot_sayisi} bot · ${y.uye_sayisi} toplam<br>${ik('mesaj', 14)} ${y.metin_kanali} metin · ${ik('ses', 14)} ${y.ses_kanali} ses`;

  const kaa = $('#kanalAyarAlan');
  if (kaa) kaa.innerHTML =
    `${ik('kalem', 14)} Log: <b>${esc(y.log_kanali || '—')}</b><br>` +
    `${ik('kapi', 14)} Giriş/Çıkış: <b>${esc(y.giris_cikis_kanali || '—')}</b><br>` +
    `${ik('yenile', 14)} 7/24: <b>${esc(y.sabit_kanal || '—')}</b><br>` +
    `${ik('kare', 14)} Üye sayaç: <b>${esc(y.uye_sayaci || '—')}</b> · Ses sayaç: <b>${esc(y.ses_sayaci || '—')}</b>`;

  const uyarilar = (y.uyarilar || []).map(u =>
    `<div class="item"><span class="t">${esc(u.kullanici)}</span><span class="s">${u.adet} uyarı</span></div>`
  ).join('') || '<div class="empty">Uyarı yok.</div>';
  const ul = $('#uyariListe'); if (ul) ul.innerHTML = uyarilar;

  const liderlik = (y.liderlik || []).map((u,i) =>
    `<div class="item"><span class="n">${i+1}.</span><span class="t">${esc(u.kullanici)}</span><span class="s">Seviye ${u.seviye} · ${u.xp} XP</span></div>`
  ).join('') || '<div class="empty">XP verisi yok.</div>';
  const ll = $('#liderlikListe'); if (ll) ll.innerHTML = liderlik;

  const voz = (durum.kanallar || []).map(c => `<option value="${c.id}">${esc(c.ad)}</option>`).join('');
  const metin = (durum.metin_kanallar || []).map(c => `<option value="${c.id}">#${esc(c.ad)}</option>`).join('');

  const doldur = (id, html, seciliAd) => {
    const el = document.getElementById(id);
    if (!el) return;
    const eski = el.value;
    el.innerHTML = html;
    if (eski && [...el.options].some(o => o.value === eski)) el.value = eski;
  };
  doldur('sel724', voz);
  doldur('selSayacKanal', voz);
  doldur('selKanalKanal', metin);
  doldur('selDuyuruKanal', metin);

  const sd = $('#sesSlider');
  const sdv = $('#sesDeger');
  if (sd && y.ses_seviyesi !== undefined && !sd.dataset.kurulu) {
    sd.value = y.ses_seviyesi;
    sd.dataset.kurulu = '1';
    if (sdv) sdv.textContent = 'Mevcut seviye: %' + y.ses_seviyesi;
    sd.oninput = () => { if (sdv) sdv.textContent = 'Seviye: %' + sd.value; };
  } else if (sdv && y.ses_seviyesi !== undefined && !sd.dataset.suruklendi) {
    sdv.textContent = 'Mevcut seviye: %' + y.ses_seviyesi;
  }
  if (sd) sd.onchange = () => { sd.dataset.suruklendi = '1'; };
}

/* ---------- karaoke: şarkı sözleri ---------- */
let sozVeri = null;
let sozAktifIdx = -1;
let sozTimer = null;
let sozOnbellek = { sorgu: null, veri: null };

async function sozOnbellekYukle(sorgu) {
  if (!sorgu || sozOnbellek.sorgu === sorgu) return;
  try {
    const j = await api('/api/sozler?sorgu=' + encodeURIComponent(sorgu));
    sozOnbellek = { sorgu: sorgu, veri: j };
  } catch {}
}

async function sozAc() {
  const kart = $('#sozKart');
  if (!kart || !sonDurum || !sonDurum.simdi) return;
  const acik = !kart.classList.contains('kapali');
  if (acik) { sozKapat(); return; }
  kart.classList.remove('kapali');
  kart.innerHTML = '<div class="soz-yuk">' + ik('mikrofon', 15) + ' Sözler aranıyor...</div>';
  const sorgu = sonDurum.simdi.sorgu;
  try {
    let j = null;
    if (sozOnbellek.sorgu === sorgu && sozOnbellek.veri) {
      j = sozOnbellek.veri;
    } else {
      j = await api('/api/sozler?sorgu=' + encodeURIComponent(sorgu));
      sozOnbellek = { sorgu: sorgu, veri: j };
    }
    if (!j.ok || !j.sozler || !j.sozler.length) throw new Error('Bu şarkı için söz bulunamadı.');
    sozVeri = j;
    sozAktifIdx = -1;
    sozKartRender(j);
    if (sozTimer) clearInterval(sozTimer);
    sozTimer = setInterval(sozTik, 500);
  } catch (e) {
    kart.innerHTML = '<div class="soz-yuk">' + ik('uyari', 15) + ' ' + esc(e.message) + '</div>';
    setTimeout(() => sozKapat(), 3200);
  }
}

function sozKapat() {
  const kart = $('#sozKart');
  if (kart) kart.classList.add('kapali');
  sozVeri = null;
  sozAktifIdx = -1;
  if (sozTimer) { clearInterval(sozTimer); sozTimer = null; }
}

function sozKartRender(v) {
  const kart = $('#sozKart');
  if (!kart) return;
  kart.innerHTML = `
    <div class="soz-bas">
      <img class="soz-kapak" src="${esc(v.kapak)}" onerror="this.style.display='none'">
      <div class="soz-meta">
        <div class="soz-ad">${esc(v.baslik)}</div>
        <div class="soz-sanatci">${esc(v.sanatci)}</div>
      </div>
      <div class="soz-ikonlar">
        <button class="soz-ikon ${sonDurum.simdi.begenildi ? 'on' : ''}" onclick="sozBegen()" title="Beğen" id="btnBegen">${ik('kalp', 16, sonDurum.simdi.begenildi)}</button>
        <button class="soz-ikon" onclick="sozKopyala()" title="Sözleri kopyala">${ik('noktalar', 16)}</button>
        <button class="soz-ikon" onclick="sozKapat()" title="Kapat">${ik('x', 16)}</button>
      </div>
    </div>
    <div class="soz-liste" id="sozListe">${v.sozler.map((s,i) =>
      `<div class="soz-line" data-i="${i}">${esc(s.metin)}</div>`).join('')}</div>`;
}

async function sozTik() {
  if (!sozVeri || !sonDurum || !sonDurum.simdi) return;
  let poz = 0;
  try {
    const j = await api('/api/pozisyon');
    if (j.simdi !== sonDurum.simdi.baslik) return;
    poz = j.pozisyon || 0;
  } catch { return; }
  const satirlar = sozVeri.sozler;
  let aktif = -1;
  for (let i = 0; i < satirlar.length; i++) {
    if (poz >= satirlar[i].start && poz < satirlar[i].end) { aktif = i; break; }
  }
  if (aktif === sozAktifIdx) return;
  sozAktifIdx = aktif;
  const liste = $('#sozListe');
  if (!liste) return;
  const satirEl = liste.children;
  for (let i = 0; i < satirEl.length; i++) {
    const fark = aktif === -1 ? 99 : Math.abs(i - aktif);
    satirEl[i].className = 'soz-line' + (i === aktif ? ' active' : fark === 1 ? ' near' : fark >= 2 ? ' far' : '');
  }
  if (aktif >= 0 && satirEl[aktif]) {
    try { satirEl[aktif].scrollIntoView({ behavior:'smooth', block:'center' }); } catch {}
  }
}

async function sozBegen() {
  if (!sonDurum || !sonDurum.simdi) return;
  const yeni = !sonDurum.simdi.begenildi;
  try {
    const j = await api('/api/begen', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({sorgu: sonDurum.simdi.sorgu, begen: yeni}) });
    sonDurum.simdi.begenildi = j.begenildi;
    const b = $('#btnBegen');
    if (b) { b.className = 'soz-ikon' + (j.begenildi ? ' on' : ''); b.innerHTML = ik('kalp', 16, j.begenildi); }
    toast((j.begenildi ? ik('kalp', 14) + ' Beğenildi' : 'Beğeni kaldırıldı'));
  } catch {}
}

async function sozKopyala() {
  if (!sozVeri || !sozVeri.sozler) return;
  try {
    const metin = sozVeri.sozler.map(s => s.metin).join('\\n');
    await navigator.clipboard.writeText(`${sozVeri.baslik}\\n${sozVeri.sanatci}\\n\\n${metin}`);
    toast(ik('kopyala', 14) + ' Sözler kopyalandı');
  } catch { toast('Kopyalanamadı', true); }
}

async function sesAyarla() {
  const sd = $('#sesSlider');
  if (!sd) return;
  try {
    const j = await api('/api/yonet', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({islem:'ses', seviye: parseInt(sd.value, 10)}) });
    if (j.hata) toast(ik('uyari', 15) + ' ' + j.hata, true); else toast(ik('ses', 15) + ' Ses seviyesi %' + sd.value + ' — sıradaki şarkıya uygulanır.');
    tazele();
  } catch {}
}

async function yonetKoruma(oz, acik) {
  try {
    await api('/api/yonet', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({islem:'koruma', ozellik:oz, acik: acik !== 'true'}) });
    tazele();
  } catch {}
}

async function yonet(islem) {
  const g = id => { const el = document.getElementById(id); return el ? el.value : null; };
  const body = { islem };
  if (islem === '724' || islem === 'sayackur') body.kanal_id = g('sel724') || g('selSayacKanal');
  if (islem === 'sayackur') body.tur = g('selSayacTur');
  if (islem === 'kanal_ayarla') {
    body.tur = g('selKanalTur');
    body.kanal_id = g('selKanalKanal');
  }
  if (islem === 'duyuru') {
    body.kanal_id = g('selDuyuruKanal');
    body.mesaj = g('duyuruMetin');
  }
  if (islem === 'genelduyuru') {
    body.mesaj = g('dmduyuruMetin');
  }
  try {
    const j = await api('/api/yonet', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) });
    if (j.hata) toast(ik('uyari', 15) + ' ' + j.hata, true);
    if (j.ok && j.gonderilen !== undefined) toast(ik('gonder', 15) + ' ' + j.gonderilen + '/' + j.toplam + ' üyeye DM ulaştı.');
    tazele();
  } catch {}
}

function cizIskeler() {
  $('#app').innerHTML = `
    <div class="stats">
      <div class="stat"><div class="s-ikon">${ik('muzik', 18)}</div><div class="s-deger" id="stSarki">—</div><div class="s-etiket">Şu An Çalan</div><div class="s-alt" id="stSarkiAlt"></div></div>
      <div class="stat"><div class="s-ikon">${ik('liste', 18)}</div><div class="s-deger" id="stSira">0</div><div class="s-etiket">Sıradaki Şarkı</div><div class="s-alt">Kuyruktaki toplam</div></div>
      <div class="stat"><div class="s-ikon">${ik('kullanicilar', 18)}</div><div class="s-deger" id="stUye">—</div><div class="s-etiket">Üye Sayısı</div><div class="s-alt" id="stUyeAlt"></div></div>
      <div class="stat"><div class="s-ikon">${ik('kupa', 18)}</div><div class="s-deger" id="stSeviye">—</div><div class="s-etiket">En Yüksek Seviye</div><div class="s-alt" id="stSeviyeAlt"></div></div>
    </div>

    <div class="card">
      <h2>${ik('kulaklik', 16)} Şimdi Çalıyor</h2>
      <div id="simdi"><div class="empty">Yükleniyor...</div></div>
      <div id="progresKart" class="progres" style="display:none">
        <div class="pbar"><div class="pbar-dolu" id="pbarDolu"></div></div>
        <div class="pbilgi"><span id="pGecen">0:00</span><span id="pToplam">0:00</span></div>
      </div>
      <div id="sozKart" class="soz-kart kapali"><div class="soz-yuk">${ik('mikrofon', 15)} Sözler için butona bas</div></div>
      <div class="controls">
        <button class="btn" id="btnSkip">${ik('sonraki', 15, true)} Geç</button>
        <button class="btn" id="btnPause">${ik('duraklat', 15, true)} Duraklat</button>
        <button class="btn" id="btnResume">${ik('oynat', 15, true)} Devam</button>
        <button class="btn ghost" id="btnStop">${ik('durdur', 15, true)} Durdur</button>
        <button class="btn ghost" id="btnKaristir">${ik('karistir', 15)} Karıştır</button>
        <button class="btn ghost" id="btnDongu">${ik('dongu', 15)} Döngü: Yok</button>
        <select class="sel" id="selChan"></select>
      </div>
    </div>

    <div class="card">
      <h2>${ik('ara', 16)} Şarkı Ara ve Ekle</h2>
      <div class="searchbar">
        <input id="q" placeholder="Şarkı adı veya YouTube linki..." autocomplete="off">
        <button class="btn" id="btnAra">Ara</button>
      </div>
      <div class="results" id="results"></div>
    </div>

    <div class="card">
      <h2>${ik('liste', 16)} Sıra <span class="say" id="siraAdet">0</span></h2>
      <div id="siraListe"><div class="empty">Yükleniyor...</div></div>
    </div>

    <div class="card" id="yonetKart">
      <h2>${ik('ayarlar', 16)} Sunucu Yönetimi</h2>
      <div id="yonet">
        <div class="yonet-grid">
          <div class="yn-block">
            <h3>${ik('kalkan', 14)} Koruma</h3>
            <p id="korumaAlan"><div class="loading">Yükleniyor...</div></p>
          </div>
          <div class="yn-block">
            <h3>${ik('grafik', 14)} Sunucu</h3>
            <p id="sunucuAlan"><div class="loading">Yükleniyor...</div></p>
          </div>
          <div class="yn-block">
            <h3>${ik('ayarlar', 14)} Kanal Ayarları</h3>
            <p id="kanalAyarAlan"><div class="loading">Yükleniyor...</div></p>
          </div>
        </div>

        <div class="yn-row">
          <div class="yn-block">
            <h3>${ik('yenile', 14)} 7/24 Ses Kanalı</h3>
            <div class="searchbar">
              <select class="sel" id="sel724"></select>
              <button class="btn" onclick="yonet('724')">Uygula</button>
            </div>
          </div>
          <div class="yn-block">
            <h3>${ik('kare', 14)} Sayaç Kur</h3>
            <div class="searchbar">
              <select class="sel" id="selSayacTur">
                <option value="uye">Üye</option>
                <option value="ses">Ses</option>
              </select>
              <select class="sel" id="selSayacKanal"></select>
              <button class="btn" onclick="yonet('sayackur')">Kur</button>
            </div>
          </div>
          <div class="yn-block">
            <h3>${ik('kalem', 14)} Kanal Ata</h3>
            <div class="searchbar">
              <select class="sel" id="selKanalTur">
                <option value="log">Log</option>
                <option value="giriscikis">Giriş/Çıkış</option>
              </select>
              <select class="sel" id="selKanalKanal"></select>
              <button class="btn" onclick="yonet('kanal_ayarla')">Ata</button>
            </div>
          </div>
        </div>

        <div class="yn-row">
          <div class="yn-block">
            <h3>${ik('megafon', 14)} Duyuru Gönder</h3>
            <div class="searchbar">
              <input id="duyuruMetin" placeholder="Duyuru mesajı..." autocomplete="off">
              <select class="sel" id="selDuyuruKanal"></select>
              <button class="btn" onclick="yonet('duyuru')">Kanal</button>
            </div>
            <div class="searchbar" style="margin-top:8px">
              <input id="dmduyuruMetin" placeholder="Tüm üyelere DM mesajı..." autocomplete="off">
              <button class="btn" onclick="yonet('genelduyuru')">${ik('gonder', 15)} DM'le</button>
            </div>
          </div>
          <div class="yn-block">
            <h3>${ik('ses', 14)} Ses Seviyesi</h3>
            <div class="searchbar">
              <input type="range" id="sesSlider" min="0" max="100" value="80">
              <button class="btn" onclick="sesAyarla()">Uygula</button>
            </div>
            <p id="sesDeger" style="margin-top:6px; font-size:12px; color:var(--muted)"></p>
          </div>
          <div class="yn-block">
            <h3>${ik('uyari', 14)} Uyarılar</h3>
            <div id="uyariListe"><div class="loading">Yükleniyor...</div></div>
          </div>
        </div>

        <div class="yn-row">
          <div class="yn-block">
            <h3>${ik('kupa', 14)} Liderlik (XP)</h3>
            <div id="liderlikListe"><div class="loading">Yükleniyor...</div></div>
          </div>
        </div>
      </div>
    </div>`;

  $('#btnSkip').onclick = async e => { e.target.disabled = true; try { await api('/api/atla',{method:'POST'}); } catch{} setTimeout(tazele,1500); };
  $('#btnPause').onclick = async e => { try { await api('/api/duraklat',{method:'POST'}); tazele(); } catch{} };
  $('#btnResume').onclick = async e => { try { await api('/api/devam',{method:'POST'}); tazele(); } catch{} };
  $('#btnStop').onclick = async e => { e.target.disabled = true; try { await api('/api/durdur',{method:'POST'}); } catch{} setTimeout(tazele,1500); };
  $('#btnKaristir').onclick = async e => { try { await api('/api/karistir',{method:'POST'}); toast(ik('karistir', 14) + ' Kuyruk karıştırıldı'); tazele(); } catch{} };
  $('#btnDongu').onclick = donguDegistir;
  $('#btnAra').onclick = ara;
  $('#q').addEventListener('keydown', e => { if (e.key === 'Enter') ara(); });
  $('#selChan').addEventListener('change', async e => {
    const id = e.target.value;
    if (!id) return;
    try {
      await api('/api/kanal', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({kanal_id: id}) });
    } catch (err) { /* hata sessiz, panel devam eder */ }
  });
}

async function ara() {
  const q = $('#q').value.trim();
  const box = $('#results');
  if (!q) return;
  box.innerHTML = '<div class="loading">Aranıyor...</div>';
  try {
    const j = await api('/api/ara?q=' + encodeURIComponent(q));
    sonSonuclar = j.sonuclar || [];
    if (!sonSonuclar.length) { box.innerHTML = '<div class="loading">Sonuç bulunamadı.</div>'; return; }
    box.innerHTML = sonSonuclar.map((s,i) => `
      <div class="res-item" onclick="ekle(${i})">
        <img src="${esc(s.thumbnail)}" onerror="this.style.display='none'">
        <div class="r-body"><div class="r-title">${esc(s.baslik)}</div>
        <div class="r-sub">${esc(s.sure)} · ${esc(s.kaynak)}</div></div>
        <div class="r-add">+ Ekle</div>
      </div>`).join('');
  } catch (e) { box.innerHTML = '<div class="loading">Arama hatası: ' + esc(e.message) + '</div>'; }
}

async function siraSil(i) {
  try {
    await api('/api/sirasil', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({indeks: i}) });
    tazele();
  } catch {}
}

async function siraTas(i, hedef) {
  if (hedef < 0) return;
  try {
    await api('/api/siratas', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({indeks: i, hedef}) });
    tazele();
  } catch {}
}

async function donguDegistir() {
  const simdiki = (sonDurum && sonDurum.dongu) || 0;
  const yeni = (simdiki + 1) % 3;
  try {
    const j = await api('/api/dongu', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({mod: yeni}) });
    toast(ik('dongu', 14) + ' Döngü: ' + ['Yok','Şarkı','Kuyruk'][j.mod]);
    tazele();
  } catch {}
}

function formatSure(sn) {
  sn = Math.max(0, Math.floor(sn || 0));
  const dk = Math.floor(sn / 60), ss = sn % 60;
  return dk + ':' + String(ss).padStart(2, '0');
}

let ilerlemeTimer = null;
function ilerlemeBaslat() { if (!ilerlemeTimer) ilerlemeTimer = setInterval(ilerlemeTik, 1000); }
function ilerlemeDurdur() { if (ilerlemeTimer) { clearInterval(ilerlemeTimer); ilerlemeTimer = null; } }
async function ilerlemeTik() {
  const kart = $('#progresKart');
  if (!kart || kart.style.display === 'none') { ilerlemeDurdur(); return; }
  let j;
  try { j = await api('/api/pozisyon'); } catch { return; }
  const gecen = $('#pGecen'); if (gecen) gecen.textContent = formatSure(j.pozisyon);
  const sureSn = sonDurum && sonDurum.simdi ? (sonDurum.simdi.sure_sn || 0) : 0;
  if (sureSn > 0) {
    const dolu = $('#pbarDolu');
    if (dolu) dolu.style.width = Math.min(100, ((j.pozisyon || 0) / sureSn) * 100) + '%';
  }
}

async function ekle(i) {
  const box = $('#results');
  const s = sonSonuclar[i];
  if (!s) return;
  box.innerHTML = '<div class="loading">Kuyruğa ekleniyor...</div>';
  try {
    const j = await api('/api/oynat', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({sorgu: s.sorgu}) });
    box.innerHTML = '<div class="loading">' + ik('onay', 15) + ' ' + esc(j.baslik || 'Eklendi') + '</div>';
    setTimeout(tazele, 1000);
  } catch (e) { box.innerHTML = '<div class="loading">Hata: ' + esc(e.message) + '</div>'; }
}

async function tazele() {
  try {
    const j = await api('/api/durum');
    if (!sonDurum) { cizPanel(j); } else { cizPanel(j); }
  } catch (e) { /* 401 yönlendirmesi api() içinde */ }
}

async function init() {
  try {
    const me = await api('/api/me');
    const logoEl = $('#logoIkon');
    if (logoEl) logoEl.innerHTML = ik('kulaklik', 26);
    $('#usermenu').innerHTML = `
      <select class="sel sunucu-sel" id="sunucuSec" title="Sunucu seç"></select>
      <img src="${esc(me.avatar)}" onerror="this.style.display='none'">
      <span>${esc(me.ad)}</span>
      <a class="btn ghost" href="/cikis">Çıkış</a>`;
    await sunuculariYukle();
    cizIskeler();
    tazele();
    setInterval(tazele, 3000);
  } catch (e) {
    renderLogin();
  }
}

async function sunuculariYukle() {
  const sel = $('#sunucuSec');
  if (!sel) return;
  try {
    const s = await api('/api/sunucular');
    sel.innerHTML = s.sunucular.map(g =>
      `<option value="${g.id}" ${g.id === s.secili ? 'selected' : ''}>${esc(g.ad)}</option>`).join('') || '<option value="">Sunucu yok</option>';
    sel.disabled = s.sunucular.length < 2;
    sel.onchange = async e => {
      if (!e.target.value) return;
      try {
        await api('/api/sunucu', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({sunucu_id: parseInt(e.target.value, 10)}) });
        sonDurum = null;
        sozKapat();
        cizIskeler();
        tazele();
        toast(ik('onay', 14) + ' Sunucu değiştirildi');
      } catch (err) {
        toast(ik('uyari', 14) + ' Sunucu değiştirilemedi', true);
        sunuculariYukle();
      }
    };
  } catch {}
}
init();
</script>
</body>
</html>
"""


async def _web_index(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Ana sayfa: oturum varsa panel, yoksa giriş ekranı."""
    if _web_cookie_al(request) is None:
        return aiohttp.web.Response(text=WEB_HTML, content_type="text/html")
    return aiohttp.web.Response(text=WEB_HTML, content_type="text/html")


async def _web_login(request: aiohttp.web.Request) -> aiohttp.web.Response:
    if _web_cookie_al(request) is not None:
        return aiohttp.web.HTTPFound("/")
    if not DISCORD_CLIENT_ID:
        return aiohttp.web.Response(text="DISCORD_CLIENT_ID ve DISCORD_CLIENT_SECRET ayarlı değil. Railway'de bu değişkenleri ekle.", content_type="text/plain")
    yetki_url = (
        "https://discord.com/api/oauth2/authorize"
        f"?client_id={DISCORD_CLIENT_ID}"
        "&response_type=code"
        "&scope=identify%20guilds"
        f"&redirect_uri={aiohttp.helpers.quote(f'{PUBLIK_URL}/callback', safe='')}"
    )
    return aiohttp.web.HTTPFound(yetki_url)


async def _web_callback(request: aiohttp.web.Request) -> aiohttp.web.Response:
    kod = request.query.get("code")
    hata = request.query.get("error")
    if hata:
        return aiohttp.web.Response(text=f"Giriş hatası: {hata}", content_type="text/plain")
    if not kod:
        return aiohttp.web.HTTPFound("/login")

    token_bilgi = await _web_oauth_token_al(kod)
    if not token_bilgi:
        return aiohttp.web.Response(text="OAuth token alınamadı.", content_type="text/plain")

    kullanici = await _web_oauth_kullanici(token_bilgi.get("access_token", ""))
    if not kullanici:
        return aiohttp.web.Response(text="Kullanıcı bilgisi alınamadı.", content_type="text/plain")

    user_id = int(kullanici["id"])

    # Kullanıcı bu botun olduğu HERHANGİ bir sunucunun üyesi mi?
    yetkili_sunucu = None
    for guild in bot.guilds:
        if guild.get_member(user_id) is not None:
            yetkili_sunucu = guild
            break

    if yetkili_sunucu is None:
        return aiohttp.web.Response(
            text="Bu paneli kullanmak için botun bulunduğu bir sunucunun üyesi olmalısın.",
            content_type="text/plain",
        )

    token = _web_oturum_kur(user_id)
    yanit = aiohttp.web.HTTPFound("/")
    yanit.set_cookie("dvrms", token, max_age=WEB_SESSION_SANIYE, httponly=True, samesite="lax")
    return yanit


async def _web_cikis(request: aiohttp.web.Request) -> aiohttp.web.Response:
    token = request.cookies.get("dvrms")
    if token:
        _web_oturumlar.pop(token, None)
        _web_oturum_tarih.pop(token, None)
    yanit = aiohttp.web.HTTPFound("/")
    yanit.del_cookie("dvrms")
    return yanit


async def _web_me(request: aiohttp.web.Request) -> aiohttp.web.Response:
    user_id = _web_cookie_al(request)
    if user_id is None:
        return aiohttp.web.json_response({"hata": "yetki"}, status=401)
    kullanici = bot.get_user(user_id)
    if kullanici is None:
        return aiohttp.web.json_response({"hata": "yetki"}, status=401)
    avatar = kullanici.display_avatar.url if kullanici.display_avatar else ""
    return aiohttp.web.json_response({"ad": kullanici.name, "avatar": avatar})


def _web_hedef_guild(request: aiohttp.web.Request) -> tuple[discord.Guild, discord.Member] | None:
    """Oturumdaki kullanıcının üyesi olduğu sunucuyu döndürür (panelde seçilene öncelik)."""
    user_id = _web_cookie_al(request)
    if user_id is None:
        return None
    token = request.cookies.get("dvrms")
    secili_id = _web_secili_sunucu.get(token) if token else None
    if secili_id:
        guild = bot.get_guild(secili_id)
        uye = guild.get_member(user_id) if guild is not None else None
        if uye is not None:
            return guild, uye
    for guild in bot.guilds:
        uye = guild.get_member(user_id)
        if uye is not None:
            return guild, uye
    return None


async def _web_sunucular(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Kullanıcının üyesi olduğu sunucuları (bot da oradaysa) listeler."""
    user_id = _web_cookie_al(request)
    if user_id is None:
        return aiohttp.web.json_response({"hata": "yetki"}, status=401)
    token = request.cookies.get("dvrms")
    secili_id = _web_secili_sunucu.get(token) if token else None
    liste = []
    for guild in bot.guilds:
        if guild.get_member(user_id) is None:
            continue
        liste.append({"id": str(guild.id), "ad": guild.name})
        if secili_id is None:
            secili_id = guild.id
    liste.sort(key=lambda g: g["ad"].lower())
    if secili_id is not None and str(secili_id) not in {g["id"] for g in liste}:
        secili_id = int(liste[0]["id"]) if liste else None
    return aiohttp.web.json_response({"sunucular": liste, "secili": str(secili_id) if secili_id else None})


async def _web_sunucu_sec(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Panelde gösterilecek sunucuyu değiştirir: POST {sunucu_id}"""
    user_id = _web_cookie_al(request)
    if user_id is None:
        return aiohttp.web.json_response({"hata": "yetki"}, status=401)
    token = request.cookies.get("dvrms")
    if not token:
        return aiohttp.web.json_response({"hata": "yetki"}, status=401)
    try:
        veri = await request.json()
    except Exception:
        return aiohttp.web.json_response({"hata": "Geçersiz istek."})
    try:
        secili_id = int(veri.get("sunucu_id"))
    except (TypeError, ValueError):
        return aiohttp.web.json_response({"hata": "Geçersiz sunucu."})
    guild = bot.get_guild(secili_id)
    if guild is None or guild.get_member(user_id) is None:
        return aiohttp.web.json_response({"hata": "Bu sunucuda değilsin."})
    _web_secili_sunucu[token] = secili_id
    return aiohttp.web.json_response({"ok": True, "sunucu": guild.name})


async def _web_durum(request: aiohttp.web.Request) -> aiohttp.web.Response:
    hedef = _web_hedef_guild(request)
    if hedef is None:
        return aiohttp.web.json_response({"hata": "yetki"}, status=401)
    guild, kullanici = hedef
    return aiohttp.web.json_response(_web_durum_json(guild, kullanici.id))


async def _web_ara(request: aiohttp.web.Request) -> aiohttp.web.Response:
    hedef = _web_hedef_guild(request)
    if hedef is None:
        return aiohttp.web.json_response({"hata": "yetki"}, status=401)
    q = request.query.get("q", "").strip()
    if not q:
        return aiohttp.web.json_response({"sonuclar": []})

    arama_ayarlari = dict(YTDLP_AYARLARI)
    arama_ayarlari["default_search"] = "ytsearch5"
    sonuclar = []
    try:
        loop = asyncio.get_running_loop()
        bilgi = await loop.run_in_executor(
            None,
            functools.partial(_sarki_ara_ayarla, arama_ayarlari, q),
        )
        if "entries" in bilgi:
            for giris in bilgi["entries"][:5]:
                if not giris:
                    continue
                sonuclar.append({
                    "baslik": giris.get("title", "Bilinmiyor"),
                    "sure": _sure_metni(giris.get("duration")),
                    "thumbnail": giris.get("thumbnail", ""),
                    "kaynak": "YouTube",
                    "sorgu": giris.get("webpage_url", ""),
                })
    except Exception as e:
        print(f"[WEB ARA] sorgu='{q}' hata: {e}")
    return aiohttp.web.json_response({"sonuclar": sonuclar})


async def _web_oynat_ep(request: aiohttp.web.Request) -> aiohttp.web.Response:
    hedef = _web_hedef_guild(request)
    if hedef is None:
        return aiohttp.web.json_response({"hata": "yetki"}, status=401)
    guild, kullanici = hedef
    try:
        veri = await request.json()
    except Exception:
        return aiohttp.web.json_response({"hata": "Geçersiz istek."})
    sorgu = (veri.get("sorgu") or "").strip()
    if not sorgu:
        return aiohttp.web.json_response({"hata": "Sorgu boş."})
    sonuc = await _web_oynat(guild, sorgu, kullanici)
    return aiohttp.web.json_response(sonuc)


async def _web_kanal(request: aiohttp.web.Request) -> aiohttp.web.Response:
    hedef = _web_hedef_guild(request)
    if hedef is None:
        return aiohttp.web.json_response({"hata": "yetki"}, status=401)
    guild, _ = hedef
    try:
        veri = await request.json()
    except Exception:
        return aiohttp.web.json_response({"hata": "Geçersiz istek."})
    kanal_id = veri.get("kanal_id")
    if not kanal_id:
        return aiohttp.web.json_response({"hata": "Kanal seçilmedi."})
    kanal = guild.get_channel(int(kanal_id))
    if not isinstance(kanal, discord.VoiceChannel):
        return aiohttp.web.json_response({"hata": "Geçersiz ses kanalı."})

    _web_secili_kanal[guild.id] = int(kanal_id)

    # Bot sesliyse her durumda (çalarken dahil) taşınır; taşınan ses kaldığı
    # yerden devam eder. Bağlı değilse seçim kaydedilir, ilk şarkıda bağlanır.
    ses_client = discord.utils.get(bot.voice_clients, guild=guild)
    if ses_client is not None and ses_client.is_connected():
        try:
            if ses_client.channel.id != kanal.id:
                await ses_client.move_to(kanal)
        except (discord.HTTPException, OSError, asyncio.TimeoutError) as e:
            return aiohttp.web.json_response({"hata": f"Taşınamadı: {e}"})

    return aiohttp.web.json_response({"ok": True, "kanal": kanal.name})


async def _web_yonet(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Sunucu yönetimi: koruma, sayaç, 7/24, kanal ayarları (sadece yönetici/moderatör)."""
    hedef = _web_hedef_guild(request)
    if hedef is None:
        return aiohttp.web.json_response({"hata": "yetki"}, status=401)
    guild, kullanici = hedef
    if not _web_kullanici_yetkili(guild, kullanici.id):
        return aiohttp.web.json_response({"hata": "Bu işlem için sunucuda yönetici/moderatör olman gerekir."}, status=403)
    try:
        veri = await request.json()
    except Exception:
        return aiohttp.web.json_response({"hata": "Geçersiz istek."})

    islem = veri.get("islem")

    if islem == "koruma":
        ozellik = veri.get("ozellik")
        acik = veri.get("acik")
        if ozellik not in KORUMA_OZELLIKLERI or not isinstance(acik, bool):
            return aiohttp.web.json_response({"hata": "Geçersiz koruma ayarı."})
        ayarlar = _veri["koruma"].setdefault(str(guild.id), {})
        ayarlar[ozellik] = acik
        _veri_kaydet()
        return aiohttp.web.json_response({"ok": True})

    if islem == "sayackur":
        tur = veri.get("tur")
        kanal_id = veri.get("kanal_id")
        if tur not in ("uye", "ses") or not kanal_id:
            return aiohttp.web.json_response({"hata": "Tür veya kanal eksik."})
        kanal = guild.get_channel(int(kanal_id))
        if not isinstance(kanal, discord.VoiceChannel):
            return aiohttp.web.json_response({"hata": "Geçersiz ses kanalı."})
        sayaclar = _veri["sayac"].setdefault(str(guild.id), {})
        sayaclar[tur] = {"kanal_id": str(kanal.id), "ad": kanal.name}
        _veri_kaydet()
        await _sayac_kanali_guncelle(kanal, tur)
        return aiohttp.web.json_response({"ok": True})

    if islem == "sayackapat":
        tur = veri.get("tur")
        sayaclar = _veri["sayac"].setdefault(str(guild.id), {})
        if tur in sayaclar:
            del sayaclar[tur]
            _veri_kaydet()
        return aiohttp.web.json_response({"ok": True})

    if islem == "724":
        kanal_id = veri.get("kanal_id")
        if not kanal_id:
            return aiohttp.web.json_response({"hata": "Kanal seçilmedi."})
        kanal = guild.get_channel(int(kanal_id))
        if not isinstance(kanal, discord.VoiceChannel):
            return aiohttp.web.json_response({"hata": "Geçersiz ses kanalı."})
        _veri.setdefault("sabit_kanal", {})[str(guild.id)] = str(kanal.id)
        _veri_kaydet()
        try:
            await _sabit_ses_kanaline_baglan(guild, zorla_tasi=True)
        except Exception as e:
            print(f"7/24 bağlanamadı ({guild.name}): {e}")
        return aiohttp.web.json_response({"ok": True})

    if islem == "kanal_ayarla":
        tur = veri.get("tur")
        kanal_id = veri.get("kanal_id")
        if tur not in ("log", "giriscikis"):
            return aiohttp.web.json_response({"hata": "Bilinmeyen kanal türü."})
        if kanal_id:
            kanal = guild.get_channel(int(kanal_id))
            if not isinstance(kanal, discord.TextChannel):
                return aiohttp.web.json_response({"hata": "Geçersiz metin kanalı."})
            _veri[tur + "_kanali"][str(guild.id)] = str(kanal.id)
        else:
            _veri[tur + "_kanali"].pop(str(guild.id), None)
        _veri_kaydet()
        return aiohttp.web.json_response({"ok": True})

    if islem == "duyuru":
        kanal_id = veri.get("kanal_id")
        mesaj = (veri.get("mesaj") or "").strip()
        if not kanal_id or not mesaj:
            return aiohttp.web.json_response({"hata": "Kanal veya mesaj eksik."})
        kanal = guild.get_channel(int(kanal_id))
        if not isinstance(kanal, discord.TextChannel):
            return aiohttp.web.json_response({"hata": "Geçersiz metin kanalı."})
        try:
            await kanal.send(mesaj)
        except discord.HTTPException as e:
            return aiohttp.web.json_response({"hata": f"Gönderilemedi: {e}"})
        return aiohttp.web.json_response({"ok": True})

    if islem == "ses":
        seviye = veri.get("seviye")
        try:
            seviye = max(0, min(100, int(seviye)))
        except (TypeError, ValueError):
            return aiohttp.web.json_response({"hata": "Geçersiz seviye."})
        _veri.setdefault("ses_seviyesi", {})[str(guild.id)] = seviye
        _veri_kaydet()
        # Mevcut şarkı yeniden başlatılmaz; yeni seviye bir sonraki şarkıda uygulanır.
        return aiohttp.web.json_response({"ok": True})

    if islem == "genelduyuru":
        mesaj = (veri.get("mesaj") or "").strip()
        if not mesaj:
            return aiohttp.web.json_response({"hata": "Duyuru mesajı boş."})
        kapali = _veri.get("duyuru_kapali", {})
        hedefler = [m for m in guild.members if not m.bot and not kapali.get(f"{guild.id}:{m.id}")]
        if not hedefler:
            return aiohttp.web.json_response({"ok": True, "gonderilen": 0, "toplam": 0})
        embed_metni = f"📣 **{guild.name}** sunucusundan duyuru:\n\n{mesaj}"
        basarili = 0
        for uye in hedefler:
            try:
                await uye.send(embed_metni)
                basarili += 1
            except discord.HTTPException:
                pass
            await asyncio.sleep(0.4)
        return aiohttp.web.json_response({"ok": True, "gonderilen": basarili, "toplam": len(hedefler)})

    return aiohttp.web.json_response({"hata": "Bilinmeyen işlem."})


async def _web_sira_sil(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Sıradan belirtilen şarkıyı kaldırır."""
    hedef = _web_hedef_guild(request)
    if hedef is None:
        return aiohttp.web.json_response({"hata": "yetki"}, status=401)
    guild, _ = hedef
    try:
        veri = await request.json()
    except Exception:
        return aiohttp.web.json_response({"hata": "Geçersiz istek."})
    indeks = veri.get("indeks")
    try:
        indeks = int(indeks)
    except (TypeError, ValueError):
        return aiohttp.web.json_response({"hata": "Geçersiz indeks."})
    sira = _sira_al(guild.id)
    if 0 <= indeks < len(sira.kuyruk):
        silinen = sira.kuyruk.pop(indeks)
        return aiohttp.web.json_response({"ok": True, "baslik": silinen.baslik})
    return aiohttp.web.json_response({"hata": "Sırada böyle bir şarkı yok."})


async def _web_atla(request: aiohttp.web.Request) -> aiohttp.web.Response:
    hedef = _web_hedef_guild(request)
    if hedef is None:
        return aiohttp.web.json_response({"hata": "yetki"}, status=401)
    guild, _ = hedef
    ses_client = discord.utils.get(bot.voice_clients, guild=guild)
    if ses_client is not None and (ses_client.is_playing() or ses_client.is_paused()):
        sira = _sira_al(guild.id)
        sira.dongu_atlama = True
        ses_client.stop()
    return aiohttp.web.json_response({"ok": True})


async def _web_duraklat(request: aiohttp.web.Request) -> aiohttp.web.Response:
    hedef = _web_hedef_guild(request)
    if hedef is None:
        return aiohttp.web.json_response({"hata": "yetki"}, status=401)
    guild, _ = hedef
    ses_client = discord.utils.get(bot.voice_clients, guild=guild)
    if ses_client is not None and ses_client.is_playing():
        sira = _sira_al(guild.id)
        sira.duraklatma_an = time.time()
        ses_client.pause()
    await _panel_guncelle(guild)
    return aiohttp.web.json_response({"ok": True})


async def _web_devam(request: aiohttp.web.Request) -> aiohttp.web.Response:
    hedef = _web_hedef_guild(request)
    if hedef is None:
        return aiohttp.web.json_response({"hata": "yetki"}, status=401)
    guild, _ = hedef
    ses_client = discord.utils.get(bot.voice_clients, guild=guild)
    if ses_client is not None and ses_client.is_paused():
        sira = _sira_al(guild.id)
        if sira.duraklatma_an is not None:
            sira.toplam_duraklatma += time.time() - sira.duraklatma_an
            sira.duraklatma_an = None
        ses_client.resume()
    await _panel_guncelle(guild)
    return aiohttp.web.json_response({"ok": True})


async def _web_durdur(request: aiohttp.web.Request) -> aiohttp.web.Response:
    hedef = _web_hedef_guild(request)
    if hedef is None:
        return aiohttp.web.json_response({"hata": "yetki"}, status=401)
    guild, _ = hedef
    sira = _sira_al(guild.id)
    sira.kuyruk.clear()
    sira.simdi_calan = None
    sira.baslama_zamani = None
    sira.duraklatma_an = None
    sira.toplam_duraklatma = 0.0
    ses_client = discord.utils.get(bot.voice_clients, guild=guild)
    if ses_client is not None:
        if ses_client.is_playing() or ses_client.is_paused():
            ses_client.stop()
        try:
            await ses_client.disconnect()
        except discord.HTTPException:
            pass
    await _panel_sil(guild)
    return aiohttp.web.json_response({"ok": True})


async def _web_dongu(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Döngü modunu ayarlar: POST {mod: 0|1|2}"""
    hedef = _web_hedef_guild(request)
    if hedef is None:
        return aiohttp.web.json_response({"hata": "yetki"}, status=401)
    guild, _ = hedef
    try:
        veri = await request.json()
    except Exception:
        veri = {}
    try:
        mod = int(veri.get("mod"))
    except (TypeError, ValueError):
        return aiohttp.web.json_response({"hata": "Geçersiz mod."})
    if mod not in (0, 1, 2):
        return aiohttp.web.json_response({"hata": "Mod 0, 1 veya 2 olmalı."})
    sira = _sira_al(guild.id)
    sira.dongu = mod
    _veri.setdefault("dongu", {})[str(guild.id)] = mod
    if mod == 2:
        sira.dongu_cevir = list(sira.kuyruk)
    elif mod == 0:
        sira.dongu_cevir = []
    _veri_kaydet()
    await _panel_guncelle(guild)
    return aiohttp.web.json_response({"ok": True, "mod": mod})


async def _web_karistir(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Sıradaki şarkıları karıştırır."""
    hedef = _web_hedef_guild(request)
    if hedef is None:
        return aiohttp.web.json_response({"hata": "yetki"}, status=401)
    guild, _ = hedef
    sira = _sira_al(guild.id)
    if len(sira.kuyruk) < 2:
        return aiohttp.web.json_response({"hata": "Karıştırmak için sırada en az 2 şarkı olmalı."})
    random.shuffle(sira.kuyruk)
    return aiohttp.web.json_response({"ok": True, "adet": len(sira.kuyruk)})


async def _web_sira_tas(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Sıradaki şarkıyı taşır: POST {indeks, hedef}"""
    hedef = _web_hedef_guild(request)
    if hedef is None:
        return aiohttp.web.json_response({"hata": "yetki"}, status=401)
    guild, _ = hedef
    try:
        veri = await request.json()
    except Exception:
        return aiohttp.web.json_response({"hata": "Geçersiz istek."})
    try:
        indeks = int(veri.get("indeks"))
        hedef_i = int(veri.get("hedef"))
    except (TypeError, ValueError):
        return aiohttp.web.json_response({"hata": "Geçersiz indeks."})
    sira = _sira_al(guild.id)
    if not (0 <= indeks < len(sira.kuyruk)) or not (0 <= hedef_i < len(sira.kuyruk)):
        return aiohttp.web.json_response({"hata": "Sıra dışı indeks."})
    tasinan = sira.kuyruk.pop(indeks)
    sira.kuyruk.insert(hedef_i, tasinan)
    return aiohttp.web.json_response({"ok": True, "baslik": tasinan.baslik})


async def _web_sozler(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Karaoke için zaman damgalı şarkı sözleri: /api/sozler?sorgu=..."""
    hedef = _web_hedef_guild(request)
    if hedef is None:
        return aiohttp.web.json_response({"hata": "yetki"}, status=401)
    sorgu = (request.query.get("sorgu") or "").strip()
    if not sorgu:
        return aiohttp.web.json_response({"hata": "Sorgu eksik."})
    onbellek = _sozler_cache.get(sorgu)
    if onbellek is None:
        try:
            onbellek = await bot.loop.run_in_executor(None, _sozleri_cek, sorgu)
            _sozler_cache[sorgu] = onbellek
        except Exception as e:
            return aiohttp.web.json_response({"hata": str(e)})
    return aiohttp.web.json_response({"ok": True, **onbellek})


async def _web_pozisyon(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Hafif pozisyon endpoint'i (karaoke senkronu sık aralıklarla bunu çeker)."""
    hedef = _web_hedef_guild(request)
    if hedef is None:
        return aiohttp.web.json_response({"hata": "yetki"}, status=401)
    guild, _ = hedef
    sira = _sira_al(guild.id)
    ses_client = discord.utils.get(bot.voice_clients, guild=guild)
    return aiohttp.web.json_response({
        "pozisyon": _sira_pozisyonu(sira),
        "caliyor": bool(ses_client and ses_client.is_playing()),
        "duraklatildi": bool(ses_client and ses_client.is_paused()),
        "simdi": sira.simdi_calan.baslik if sira.simdi_calan is not None else None,
    })


async def _web_begen(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """Şarkı beğenme durumunu kaydeder: POST {sorgu, begen: bool}"""
    hedef = _web_hedef_guild(request)
    if hedef is None:
        return aiohttp.web.json_response({"hata": "yetki"}, status=401)
    guild, _ = hedef
    try:
        veri = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        veri = {}
    sorgu = (veri.get("sorgu") or "").strip()
    if not sorgu:
        return aiohttp.web.json_response({"hata": "Sorgu eksik."})
    begen = bool(veri.get("begen"))
    _veri.setdefault("begenilenler", {})[f"{guild.id}:{sorgu}"] = begen
    _veri_kaydet()
    return aiohttp.web.json_response({"ok": True, "begenildi": begen})


async def _web_baslat():
    """aiohttp web sunucusunu bot'un event loop'unda başlatır."""
    if aiohttp is None:
        print("aiohttp kurulu değil; web paneli devre dışı.")
        return
    app = aiohttp.web.Application()
    app.router.add_get("/", _web_index)
    app.router.add_get("/login", _web_login)
    app.router.add_get("/callback", _web_callback)
    app.router.add_get("/cikis", _web_cikis)
    app.router.add_get("/api/me", _web_me)
    app.router.add_get("/api/durum", _web_durum)
    app.router.add_get("/api/ara", _web_ara)
    app.router.add_post("/api/oynat", _web_oynat_ep)
    app.router.add_post("/api/kanal", _web_kanal)
    app.router.add_post("/api/yonet", _web_yonet)
    app.router.add_post("/api/atla", _web_atla)
    app.router.add_post("/api/sirasil", _web_sira_sil)
    app.router.add_post("/api/duraklat", _web_duraklat)
    app.router.add_post("/api/devam", _web_devam)
    app.router.add_post("/api/durdur", _web_durdur)
    app.router.add_get("/api/sozler", _web_sozler)
    app.router.add_get("/api/pozisyon", _web_pozisyon)
    app.router.add_post("/api/begen", _web_begen)
    app.router.add_post("/api/dongu", _web_dongu)
    app.router.add_post("/api/karistir", _web_karistir)
    app.router.add_post("/api/siratas", _web_sira_tas)
    app.router.add_get("/api/sunucular", _web_sunucular)
    app.router.add_post("/api/sunucu", _web_sunucu_sec)

    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "0.0.0.0", WEB_PORT)
    await site.start()
    print(f"Web paneli çalışıyor: {PUBLIK_URL}")


if __name__ == "__main__":
    _veri_yukle()
    bot.run(TOKEN)
