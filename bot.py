import os
import re
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
}

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
            await kanal.set_permissions(
                rol, view_channel=False, reason="üye rolünden kanalı gizle"
            )
        except discord.HTTPException:
            pass

    return rol


intents = discord.Intents.default()
intents.voice_states = True
intents.members = True
intents.message_content = True  # isim mesajını okuyabilmek için gerekli

bot = commands.Bot(command_prefix="!", intents=intents)

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
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"{len(synced)} slash komut senkronize edildi.")
    except Exception as e:
        print(f"Senkron hatası: {e}")

    for guild in bot.guilds:
        try:
            await _uye_rolu_getir_veya_olustur(guild)
        except discord.HTTPException as e:
            print(f"'{UYE_ROLU_ADI}' rolü hazırlanamadı ({guild.name}): {e}")

    print(f"{bot.user} olarak giriş yapıldı.")


@bot.event
async def on_member_join(member: discord.Member):
    guild = member.guild

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

    kanallar = [
        vc for vc in interaction.guild.voice_channels
        if vc.permissions_for(interaction.guild.me).connect
    ]

    if len(kanallar) < 2:
        await interaction.response.send_message(
            "Gezdirmek için en az 2 tane erişilebilir ses kanalı olmalı.", ephemeral=True
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
@play.error
@skip.error
@sira_goster.error
@kuyruktemizle.error
@duraklat.error
@devam.error
@dur.error
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
    bot.run(TOKEN)
