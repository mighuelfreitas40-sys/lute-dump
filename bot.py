import asyncio
import os
import re
import time
import pathlib
import subprocess
from datetime import datetime

import aiohttp
import discord
from discord.ext import commands
from discord import app_commands

# ------------------------------------------------------------------ config
TOKEN      = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = 1523302337937146017
LOGS_ID    = 1523302467553464403
TIMEOUT    = 100
MAX_DL     = 8 * 1024 * 1024

ROOT = pathlib.Path(__file__).resolve().parent
LUNE = ROOT / "lune"
LUTE = ROOT / "lute"
TMP  = ROOT / "bot_tmp"
TMP.mkdir(exist_ok=True)

ACCENT  = 0x5865F2
GOOD    = 0x57F287
BAD     = 0xED4245
WARN    = 0xFEE75C

URL_RE  = re.compile(r"https?://[^\s<>()]+", re.I)
TIME_RE = re.compile(r"Finished processing in ([\d.]+) seconds", re.I)
OK_EXT  = (".lua", ".txt")

# ------------------------------------------------------------------ engine
def _dump_blocking(in_rel: str, out_rel: str):
    env = os.environ.copy()
    env["HOOKOP_BIN"] = str(LUTE)

    started = time.perf_counter()
    proc = subprocess.Popen(
        [str(LUNE), "run", "main.luau", in_rel, f"out={out_rel}"],
        cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        log, _ = proc.communicate(timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        try: proc.communicate(timeout=5)
        except Exception: pass
        return False, "timeout", TIMEOUT

    took = time.perf_counter() - started
    m = TIME_RE.search(log or "")
    if m:
        took = float(m.group(1))

    out_path = ROOT / out_rel
    if proc.returncode != 0 or not out_path.exists():
        tail = (log or "").strip().splitlines()[-1:] or ["unknown error"]
        return False, tail[-1][:300], took

    head = out_path.read_text(errors="ignore")[:6]
    if head.startswith("--err"):
        reason = out_path.read_text(errors="ignore")[5:].strip()
        return False, reason[:300] or "engine error", took

    return True, None, took

# ------------------------------------------------------------------ bot
class DeobfBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.http_session: aiohttp.ClientSession | None = None
        self.queue: asyncio.Queue[dict] = asyncio.Queue()

    async def setup_hook(self):
        self.http_session = aiohttp.ClientSession()
        self.loop.create_task(self._worker())
        guild = discord.Object(id=1523302337937146017)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)

    async def on_ready(self):
        await self.change_presence(activity=discord.Activity(
            type=discord.ActivityType.watching, name="/deobf · dumps"))
        print(f"online as {self.user}")

    async def _fetch_source(self, job: dict) -> str:
        if job["att"] is not None:
            return (await job["att"].read()).decode("utf-8", "ignore")
        async with self.http_session.get(job["url"], timeout=aiohttp.ClientTimeout(total=30)) as r:
            r.raise_for_status()
            chunks, total = [], 0
            async for part in r.content.iter_chunked(65536):
                total += len(part)
                if total > MAX_DL:
                    raise ValueError("file too large")
                chunks.append(part)
            return b"".join(chunks).decode("utf-8", "ignore")

    async def _send_log(self, job: dict, ok: bool, reason: str = None, took: float = 0):
        log_ch = self.get_channel(LOGS_ID)
        if not log_ch:
            return

        msg = job["message"]
        name = job["name"]
        user = msg.user

        color = GOOD if ok else (WARN if reason == "timeout" else BAD)
        e = discord.Embed(color=color, timestamp=datetime.now())
        e.set_author(name=f"{user}", icon_url=user.display_avatar.url)
        e.add_field(name="Usuário", value=f"{user.mention} (`{user.id}`)", inline=False)
        e.add_field(name="Arquivo", value=f"`{name}`", inline=True)
        e.add_field(name="Status", value="✅ Sucesso" if ok else f"❌ {reason}", inline=True)
        e.add_field(name="Tempo", value=f"`{took:.2f}s`", inline=True)

        if ok:
            out_path = job.get("out_path")
            if out_path and out_path.exists():
                with open(out_path, "rb") as fh:
                    await log_ch.send(embed=e, file=discord.File(fh, filename=name.replace(".lua", ".dump.lua")))
                return

        await log_ch.send(embed=e)

    async def _worker(self):
        await self.wait_until_ready()
        while True:
            job = await self.queue.get()
            interaction, name = job["interaction"], job["name"]
            stamp = f"{int(time.time()*1000)}_{os.getpid()}"
            in_rel  = f"bot_tmp/{stamp}.lua"
            out_rel = f"bot_tmp/{stamp}_out.lua"
            in_path, out_path = ROOT / in_rel, ROOT / out_rel
            job["out_path"] = out_path

            try:
                src = await self._fetch_source(job)
                in_path.write_text(src, encoding="utf-8", errors="ignore")

                ok, reason, took = await asyncio.to_thread(_dump_blocking, in_rel, out_rel)

                if ok:
                    data = out_path.read_text(errors="ignore")
                    out_name = re.sub(r"\.(lua|txt)$", "", name, flags=re.I) + ".dump.lua"
                    with open(out_path, "rb") as fh:
                        try:
                            await interaction.user.send(
                                content="Script desfuscado com sucesso, e enviado na sua dm",
                                file=discord.File(fh, filename=out_name),
                            )
                        except discord.Forbidden:
                            await interaction.followup.send(
                                "Não consegui enviar DM. Verifique suas configurações de privacidade.",
                                ephemeral=True,
                            )

                    await self._send_log(job, True, took=took)
                else:
                    label = "skipped — took over 100s" if reason == "timeout" else reason
                    await self._send_log(job, False, reason=reason, took=took)

            except Exception as ex:
                await self._send_log(job, False, reason=str(ex)[:300], took=0)
            finally:
                for p in (in_path, out_path):
                    try: p.unlink()
                    except OSError: pass
                self.queue.task_done()


bot = DeobfBot()


def _gather_jobs(interaction: discord.Interaction, attachment: discord.Attachment = None, url: str = None) -> list[dict]:
    jobs = []
    if attachment:
        name = attachment.filename
        if not name.lower().endswith(OK_EXT):
            name += ".lua"
        jobs.append({"name": name, "att": attachment, "url": None})
    if url:
        name = url.split("?")[0].rstrip("/").split("/")[-1] or "script"
        if not name.lower().endswith(OK_EXT):
            name += ".lua"
        jobs.append({"name": name, "att": None, "url": url})
    return jobs


@app_commands.command(name="deobf", description="Deobfusca código Lua")
@app_commands.describe(attachment="Arquivo .lua ou .txt", url="Link raw do script")
async def deobf(interaction: discord.Interaction, attachment: discord.Attachment = None, url: str = None):
    if not attachment and not url:
        await interaction.response.send_message(
            "Anexe um arquivo ou forneça um URL.", ephemeral=True
        )
        return

    jobs = _gather_jobs(interaction, attachment, url)
    if not jobs:
        await interaction.response.send_message(
            "Nenhum script válido encontrado.", ephemeral=True
        )
        return

    await interaction.response.send_message(
        f"Processando `{len(jobs)}` script(s)...", ephemeral=True
    )

    for j in jobs:
        j["interaction"] = interaction
        await bot.queue.put(j)


bot.tree.add_command(deobf)


if __name__ == "__main__":
    bot.run(TOKEN)
