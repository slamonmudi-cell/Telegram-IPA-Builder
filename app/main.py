from __future__ import annotations

import asyncio
import logging
import tempfile
import uuid
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlparse

import httpx
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from .archive import UnsafeArchive, extract_project, locate_project_root
from .config import Settings
from .github_client import GitHubBuilder, GitHubError

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
LOGGER = logging.getLogger(__name__)
SETTINGS = Settings.from_env()
BUILD_LOCK = asyncio.Semaphore(2)
ALLOWED_DOWNLOAD_HOSTS = {
    "github.com",
    "api.github.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
    "raw.githubusercontent.com",
    "release-assets.githubusercontent.com",
}


def _authorized(update: Update) -> bool:
    user = update.effective_user
    return bool(
        user
        and (
            not SETTINGS.allowed_user_ids
            or user.id in SETTINGS.allowed_user_ids
        )
    )


async def start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        await update.effective_message.reply_text("هذا البوت خاص.")
        return
    await update.effective_message.reply_text(
        "أرسل مشروع SwiftUI/XcodeGen بصيغة ZIP وسأبني لك IPA غير موقّع. "
        "يجب أن يحتوي الملف على project.yml وAssets.xcassets."
    )


async def whoami(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user:
        await update.effective_message.reply_text(f"Telegram user ID: {user.id}")


async def build_zip(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    document = message.document
    if not _authorized(update):
        await message.reply_text("غير مصرح لك باستخدام هذا البوت.")
        return
    if not document or not document.file_name.lower().endswith(".zip"):
        await message.reply_text("أرسل ملف ZIP فقط.")
        return
    if document.file_size and document.file_size > SETTINGS.max_zip_mb * 1024 * 1024:
        await message.reply_text(f"حجم ZIP يجب ألا يتجاوز {SETTINGS.max_zip_mb} MB.")
        return

    async def download_to(zip_path: Path) -> None:
        telegram_file = await document.get_file()
        await telegram_file.download_to_drive(zip_path)

    await _build_project(update, download_to)


async def build_url(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not _authorized(update):
        await message.reply_text("غير مصرح لك باستخدام هذا البوت.")
        return

    url = (message.text or "").strip()
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() not in ALLOWED_DOWNLOAD_HOSTS
        or not parsed.path.lower().endswith(".zip")
    ):
        await message.reply_text("أرسل رابط تنزيل مباشر لملف ZIP من GitHub Releases.")
        return

    async def download_to(zip_path: Path) -> None:
        await _download_zip_url(url, zip_path)

    await _build_project(update, download_to)


async def _download_zip_url(url: str, destination: Path) -> None:
    limit = SETTINGS.max_zip_mb * 1024 * 1024
    total = 0
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(120.0, connect=20.0),
    ) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            final_host = (response.url.host or "").lower()
            if final_host not in ALLOWED_DOWNLOAD_HOSTS:
                raise ValueError("رابط التحويل النهائي ليس من GitHub.")
            content_length = response.headers.get("content-length")
            if content_length and int(content_length) > limit:
                raise ValueError(f"حجم ZIP يجب ألا يتجاوز {SETTINGS.max_zip_mb} MB.")
            with destination.open("wb") as output:
                async for chunk in response.aiter_bytes(1024 * 1024):
                    total += len(chunk)
                    if total > limit:
                        raise ValueError(
                            f"حجم ZIP يجب ألا يتجاوز {SETTINGS.max_zip_mb} MB."
                        )
                    output.write(chunk)


async def _build_project(
    update: Update,
    download_to: Callable[[Path], Awaitable[None]],
) -> None:
    message = update.effective_message
    job_id = uuid.uuid4().hex[:12]
    status = await message.reply_text(f"بدأ الطلب `{job_id}`… جاري تنزيل المشروع.", parse_mode="Markdown")

    async with BUILD_LOCK:
        branch: str | None = None
        try:
            with tempfile.TemporaryDirectory(prefix=f"ipa-{job_id}-") as temp:
                temp_path = Path(temp)
                zip_path = temp_path / "project.zip"
                await message.chat.send_action(ChatAction.TYPING)
                await download_to(zip_path)

                project_extract = temp_path / "project"
                extract_project(
                    zip_path,
                    project_extract,
                    max_unpacked_bytes=SETTINGS.max_unpacked_mb * 1024 * 1024,
                    max_files=SETTINGS.max_files,
                )
                project_root = locate_project_root(project_extract)
                await status.edit_text("تم فحص المشروع. جاري رفعه إلى فرع بناء مؤقت…")

                async with GitHubBuilder(
                    SETTINGS.github_token,
                    SETTINGS.github_owner,
                    SETTINGS.github_repo,
                    SETTINGS.github_default_branch,
                ) as github:
                    branch = await github.create_build_branch(job_id, project_root)
                    started_at = await github.dispatch(branch, job_id)
                    await status.edit_text("بدأ البناء على macOS في GitHub Actions. قد يستغرق عدة دقائق…")

                    run = await github.wait_for_run(
                        branch,
                        started_at,
                        timeout_seconds=SETTINGS.build_timeout_minutes * 60,
                    )
                    if run.get("conclusion") != "success":
                        url = run.get("html_url", "")
                        raise GitHubError(f"فشل البناء. سجل الأخطاء: {url}")

                    output = temp_path / "output"
                    files = await github.download_artifact(run["id"], output)
                    await github.delete_branch(branch)
                    branch = None

                    ipa = next((path for path in files if path.suffix.lower() == ".ipa"), None)
                    preview = next(
                        (path for path in files if path.name.lower().endswith("preview.png")),
                        None,
                    )
                    if ipa is None:
                        raise GitHubError("نجح البناء لكن ملف IPA غير موجود")

                    if preview:
                        with preview.open("rb") as preview_file:
                            await message.reply_photo(
                                photo=preview_file,
                                caption=f"معاينة التطبيق — الطلب {job_id}",
                            )

                    if ipa.stat().st_size > SETTINGS.telegram_max_upload_mb * 1024 * 1024:
                        await status.edit_text(
                            f"اكتمل البناء، لكن حجم IPA أكبر من حد إرسال البوت "
                            f"({SETTINGS.telegram_max_upload_mb} MB). النتيجة: {run['html_url']}"
                        )
                        return

                    await message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)
                    with ipa.open("rb") as ipa_file:
                        await message.reply_document(
                            document=ipa_file,
                            filename=f"{job_id}-unsigned.ipa",
                            caption="تم البناء بنجاح. هذا IPA غير موقّع.",
                        )
                    await status.edit_text("اكتمل البناء والإرسال بنجاح ✅")

        except (
            UnsafeArchive,
            GitHubError,
            httpx.HTTPError,
            OSError,
            ValueError,
        ) as exc:
            LOGGER.exception("Build %s failed", job_id)
            await status.edit_text(f"تعذر إكمال البناء:\n{str(exc)[:3500]}")
        except Exception:
            LOGGER.exception("Unexpected build error for %s", job_id)
            await status.edit_text("حدث خطأ غير متوقع. راجع سجل البوت.")
        finally:
            if branch:
                async with GitHubBuilder(
                    SETTINGS.github_token,
                    SETTINGS.github_owner,
                    SETTINGS.github_repo,
                    SETTINGS.github_default_branch,
                ) as github:
                    await github.delete_branch(branch)


def main() -> None:
    application = Application.builder().token(SETTINGS.telegram_token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("whoami", whoami))
    application.add_handler(MessageHandler(filters.Document.ALL, build_zip))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, build_url))
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
