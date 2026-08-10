import os
import re
import json
import random
import time
import asyncio
import itertools
import functools
import datetime

import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp

# ============================================
# AYARLAR
# ============================================
TOKEN = os.getenv("DISCORD_TOKEN", "BOT_TOKENINI_BURAYA_YAZ")

# Windows'ta ffmpeg.exe PATH'te değilse buraya tam yolunu yazabilirsin,
# örn: r"C:\ffmpeg\bin\ffmpeg.exe"
FFMPEG_YOLU = "ffmpeg"

YTDLP_AYARLARI = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch1",
    "source_address": "0.0.0.0",
    # YouTube'un "Sign in to confirm you're not a bot" kontrolünü atlatmak için
    # Android/web istemcisi gibi davranıyoruz. Garanti değil (YouTube sık değişiyor)
    # ama datacenter IP'lerde (Railway vb.) en çok işe yarayan yöntem bu.
    "extractor_args": {
        "youtube": {
            "player_client": ["android", "web"],
        }
    },
}

# Opsiyonel: bir cookies.txt dosyan varsa (Netscape formatında, YouTube'a giriş
# yapılmış bir hesaptan export edilmiş), COOKIES_DOSYASI ortam değişkenine dosya
# yolunu yazarsan otomatik kullanılır ve bot tespiti çok daha az tetiklenir.
COOKIES_DOSYASI = os.getenv("COOKIES_DOSYASI", "")
if COOKIES_DOSYASI and os.path.exists(COOKIES_DOSYASI):
    YTDLP_AYARLARI["cookiefile"] = COOKIES_DOSYASI

FFMPEG_SECENEKLERI = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

# Sunucuya yeni girenlere otomatik verilecek rol. Bu rol tüm normal
# kanallardan gizlenir; sadece bot'un açtığı kişiye özel kanal görünür.
UYE_ROLU_ADI = "Üye"

# Renk rolü seçim emojileri -> (görünen isim, hex renk)
RENK_EMOJILERI = {
    "🔴": ("Kırmızı", 0xE74C3C),
    "🟠": ("Turuncu", 0xE67E22),
    "🟡": ("Sarı", 0xF1C40F),
    "🟢": ("Yeşil", 0x2ECC71),
    "🔵": ("Mavi", 0x3498DB),
    "🟣": ("Mor", 0x9B59B6),
    "🩷": ("Pembe", 0xFF6FCF),
    "⚪": ("Beyaz", 0xFFFFFF),
    "⚫": ("Siyah", 0x4F545C),
}

_TR_CEVIRI = str.maketrans({
    "ı": "i", "İ": "i", "ş": "s", "Ş": "s", "ğ": "g", "Ğ": "g",
    "ü": "u", "Ü": "u", "ö": "o", "Ö": "o", "ç": "c", "Ç": "c",
})


def _kanal_adi_olustur(uye: discord.Member) -> str:
    """Discord kanal ismi kurallarına uygun (küçük harf, tire, türkçe karaktersiz) isim üretir."""
    ad = uye.name.lower().translate(_TR_CEVIRI)
    ad = re.sub(r"[^a-z0-9-]", "-", ad)
    ad = re.sub(r"-+", "-", ad).strip("-")
    if not ad:
        ad = f"uye-{uye.id}"
    return ad[:90]


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
        "koruma": {},      # "guild_id" -> {"link": bool, "kufur": bool, "spam": bool, "yenihesap": bool}
        "cekilisler": {},  # "mesaj_id" -> çekiliş kaydı
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


# ============================================
# ANTI-SPAM / ANTI-LINK KORUMASI
# ============================================
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

    # Devam eden çekilişlerin butonlarını ve süre sayaçlarını geri yükle
    for mid_str, kayit in _veri["cekilisler"].items():
        if kayit.get("bitti"):
            continue
        try:
            bot.add_view(CekilisView(int(mid_str)))
        except Exception as e:
            print(f"Çekiliş view'ı eklenemedi ({mid_str}): {e}")
        cekilis_gorevleri[int(mid_str)] = bot.loop.create_task(_cekilis_sayaci(int(mid_str)))

    # Ses XP döngüsünü bir kez başlat (reconnect'lerde tekrar başlatma)
    if not getattr(bot, "_ses_xp_basladi", False):
        bot._ses_xp_basladi = True
        bot.loop.create_task(ses_xp_dongusu())

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

    # --- 0) 'Üye' rolünü ver: bu rol tüm normal kanallardan gizlidir,
    # kişi sadece bot'un az sonra açacağı özel karşılama kanalını görebilir ---
    try:
        uye_rolu = await _uye_rolu_getir_veya_olustur(guild)
        await member.add_roles(uye_rolu, reason="sunucuya giriş - otomatik üye rolü")
    except discord.HTTPException as e:
        print(f"Üye rolü verilemedi ({member}): {e}")

    # --- 1) Sadece bu üyenin göreceği özel kanalı oluştur ---
    # Not: "Administrator" yetkisine sahip roller/yöneticiler kanal izin
    # ayarlarını (overwrite) tamamen atlar, yani @everyone'dan gizlesek
    # bile yöneticiler bu kanalı zaten görebilir.
    temel_ad = _kanal_adi_olustur(member)
    kanal_adi = temel_ad
    if discord.utils.get(guild.text_channels, name=kanal_adi) is not None:
        kanal_adi = f"{temel_ad}-{str(member.id)[-4:]}"

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        member: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True, add_reactions=True
        ),
    }

    try:
        kanal = await guild.create_text_channel(
            kanal_adi, overwrites=overwrites, reason="karşılama - renk/rol seçim kanalı"
        )
    except discord.HTTPException as e:
        print(f"Karşılama kanalı oluşturulamadı ({member}): {e}")
        return

    # --- 2) Renk seçimi (reaksiyon) + isim (yazılı mesaj) iste ---
    renk_listesi = "\n".join(f"{emoji} — {isim}" for emoji, (isim, _) in RENK_EMOJILERI.items())
    mesaj = await kanal.send(
        f"{member.mention} hoş geldin! 🎉\n\n"
        f"**1)** Rolünün rengi için aşağıdaki emojilerden birine tıkla:\n{renk_listesi}\n\n"
        f"**2)** Rolüne vermek istediğin **ismi** bu kanala yazarak gönder.\n\n"
        f"İkisini de yaptığında rolün otomatik oluşturulup sana verilecek. (10 dakika içinde tamamla)"
    )
    for emoji in RENK_EMOJILERI:
        try:
            await mesaj.add_reaction(emoji)
        except discord.HTTPException:
            pass

    def reaksiyon_kontrol(payload: discord.RawReactionActionEvent) -> bool:
        return (
            payload.message_id == mesaj.id
            and payload.user_id == member.id
            and str(payload.emoji) in RENK_EMOJILERI
        )

    def mesaj_kontrol(m: discord.Message) -> bool:
        return m.channel.id == kanal.id and m.author.id == member.id

    BEKLEME_SANIYE = 600  # 10 dakika

    secilen_renk_hex = None
    secilen_isim = None

    renk_task = asyncio.create_task(
        bot.wait_for("raw_reaction_add", check=reaksiyon_kontrol, timeout=BEKLEME_SANIYE)
    )
    isim_task = asyncio.create_task(
        bot.wait_for("message", check=mesaj_kontrol, timeout=BEKLEME_SANIYE)
    )
    bekleyenler = {renk_task, isim_task}

    while bekleyenler:
        tamamlanan, bekleyenler = await asyncio.wait(bekleyenler, return_when=asyncio.FIRST_COMPLETED)
        for gorev in tamamlanan:
            try:
                sonuc = gorev.result()
            except asyncio.TimeoutError:
                for kalan in bekleyenler:
                    kalan.cancel()
                await kanal.send("⏰ Süre doldu, işlem iptal edildi. Bu kanal birazdan silinecek.")
                await asyncio.sleep(10)
                try:
                    await kanal.delete(reason="rol seçimi zaman aşımı")
                except discord.HTTPException:
                    pass
                return

            if gorev is renk_task:
                _, secilen_renk_hex = RENK_EMOJILERI[str(sonuc.emoji)]
            else:
                secilen_isim = sonuc.content.strip()[:100]

    if not secilen_isim:
        secilen_isim = member.display_name

    # --- 3) Rolü oluştur, kişiye özel renkte, üye listesinde ayrı gösterilecek (hoist) ---
    try:
        rol = await guild.create_role(
            name=secilen_isim,
            color=discord.Color(secilen_renk_hex),
            hoist=True,
            reason=f"{member} için renk rolü",
        )
        await member.add_roles(rol, reason="renk rolü ataması")
    except discord.HTTPException as e:
        await kanal.send(f"⚠️ Rol oluşturulamadı: {e}")
        return

    # --- 3.1) Özel rol verildi, artık geçici 'Üye' rolüne gerek yok: kaldır ---
    uye_rolu = discord.utils.get(guild.roles, name=UYE_ROLU_ADI)
    if uye_rolu is not None and uye_rolu in member.roles:
        try:
            await member.remove_roles(uye_rolu, reason="özel rol verildi - üye rolü kaldırıldı")
        except discord.HTTPException as e:
            print(f"Üye rolü kaldırılamadı ({member}): {e}")

    await kanal.send(
        f"✅ **{rol.name}** rolü oluşturuldu ve sana verildi! Bu kanal 10 saniye içinde silinecek."
    )
    await asyncio.sleep(10)
    try:
        await kanal.delete(reason="rol seçimi tamamlandı")
    except discord.HTTPException:
        pass


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

    def __init__(self, sorgu: str, baslik: str, isteyen: discord.Member, kanal: discord.abc.Messageable):
        self.sorgu = sorgu
        self.baslik = baslik
        self.isteyen = isteyen
        self.kanal = kanal


class MuzikSirasi:
    def __init__(self):
        self.kuyruk: list[SarkiKaydi] = []
        self.simdi_calan: SarkiKaydi | None = None


muzik_siralari: dict[int, MuzikSirasi] = {}  # guild_id -> MuzikSirasi


def _sira_al(guild_id: int) -> MuzikSirasi:
    return muzik_siralari.setdefault(guild_id, MuzikSirasi())


# ============================================
# 7/24 SESSİZ SES KANALINDA BEKLEME
# ============================================
# Bot, sunucudaki en üstteki (ilk) ses kanalına girer, kendini sessize alır
# (self-mute + self-deaf) ve orada kalır. Biri onu atarsa/kanaldan çıkarsa
# otomatik olarak geri bağlanır. Müzik çalınca oraya taşınır, müzik bitince
# tekrar sabit kanala döner.
async def _sabit_ses_kanaline_baglan(guild: discord.Guild, zorla_tasi: bool = False) -> None:
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
    """Bloklayıcı yt-dlp aramasını çalıştırır (executor içinde çağrılmalı)."""
    with yt_dlp.YoutubeDL(YTDLP_AYARLARI) as ydl:
        bilgi = ydl.extract_info(sorgu, download=False)
        if "entries" in bilgi:
            bilgi = bilgi["entries"][0]
        return bilgi


def _sarki_bitince(guild: discord.Guild, hata: Exception | None):
    """FFmpeg oynatma bitince (ayrı bir thread'den) çağrılır; sıradaki şarkıyı başlatır."""
    if hata:
        print(f"Oynatma hatası: {hata}")
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

    if not sira.kuyruk:
        sira.simdi_calan = None
        # Çalınacak şarkı kalmadı, botu tekrar sabit 7/24 ses kanalına gönder.
        try:
            await _sabit_ses_kanaline_baglan(guild, zorla_tasi=True)
        except Exception as e:
            print(f"Müzik bitince sabit kanala dönülemedi ({guild.name}): {e}")
        return

    sonraki = sira.kuyruk.pop(0)

    try:
        loop = asyncio.get_running_loop()
        bilgi = await loop.run_in_executor(None, functools.partial(_sarki_ara, sonraki.sorgu))
        stream_url = bilgi.get("url")
    except Exception as e:
        try:
            await sonraki.kanal.send(f"⚠️ **{sonraki.baslik}** çalınamadı, atlanıyor: {e}")
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

    sira.simdi_calan = sonraki
    kaynak = discord.FFmpegPCMAudio(stream_url, executable=FFMPEG_YOLU, **FFMPEG_SECENEKLERI)
    ses_client.play(kaynak, after=lambda e, g=guild: _sarki_bitince(g, e))

    try:
        await sonraki.kanal.send(f"🎵 Şimdi çalıyor: **{sonraki.baslik}** — istek: {sonraki.isteyen.mention}")
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
        await interaction.followup.send(f"Şarkı bulunamadı/işlenemedi: {e}")
        return

    baslik = bilgi.get("title", sarki)
    sorgu = bilgi.get("webpage_url", sarki)

    sira = _sira_al(interaction.guild.id)
    kayit = SarkiKaydi(sorgu=sorgu, baslik=baslik, isteyen=interaction.user, kanal=interaction.channel)
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
    await interaction.response.send_message("⏸️ Duraklatıldı.")


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
    await interaction.response.send_message("▶️ Devam ediyor.")


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

    await interaction.response.send_message("⏹️ Müzik durduruldu, kuyruk temizlendi, ses kanalından ayrıldım.")


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
# LEVEL / XP KOMUTLARI
# ============================================

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
async def komut_hata(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "Bu komutu kullanmak için gerekli sunucu yetkisine sahip değilsin.", ephemeral=True
        )
    else:
        if not interaction.response.is_done():
            await interaction.response.send_message(f"Bir hata oluştu: {error}", ephemeral=True)
        print(f"Komut hatası: {error}")


if __name__ == "__main__":
    _veri_yukle()
    bot.run(TOKEN)
