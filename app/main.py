from __future__ import annotations

import asyncio
import logging
import tempfile
import uuid
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.error import NetworkError, TimedOut
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest

from .archive import UnsafeArchive, extract_project, locate_project_root
from .config import Settings
from .github_client import GitHubBuilder, GitHubError

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
LOGGER = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
SETTINGS = Settings.from_env()
BUILD_LOCK = asyncio.Semaphore(2)


async def _send_preview(message, preview: Path, job_id: str) -> bool:
    for attempt in range(1, 4):
        try:
            with preview.open("rb") as preview_file:
                await message.reply_photo(
                    photo=preview_file,
                    caption=f"معاينة التطبيق — الطلب {job_id}",
                    read_timeout=120,
                    write_timeout=180,
                    connect_timeout=30,
                    pool_timeout=30,
                )
            return True
        except (TimedOut, NetworkError) as exc:
            LOGGER.warning("Preview upload attempt %s failed: %s", attempt, type(exc).__name__)
            if attempt < 3:
                await asyncio.sleep(attempt * 3)
    return False


async def _send_ipa(message, ipa: Path, job_id: str) -> None:
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            with ipa.open("rb") as ipa_file:
                await message.reply_document(
                    document=ipa_file,
                    filename=f"{job_id}-unsigned.ipa",
                    caption="تم البناء بنجاح. هذا IPA غير موقّع.",
                    read_timeout=180,
                    write_timeout=300,
                    connect_timeout=30,
                    pool_timeout=30,
                )
            return
        except (TimedOut, NetworkError) as exc:
            last_error = exc
            LOGGER.warning("IPA upload attempt %s failed: %s", attempt, type(exc).__name__)
            if attempt < 3:
                await asyncio.sleep(attempt * 5)
    raise TimedOut("Telegram could not receive the IPA after three attempts") from last_error


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

    job_id = uuid.uuid4().hex[:12]
    status = await message.reply_text(f"بدأ الطلب `{job_id}`… جاري تنزيل المشروع.", parse_mode="Markdown")

    async with BUILD_LOCK:
        branch: str | None = None
        try:
            with tempfile.TemporaryDirectory(prefix=f"ipa-{job_id}-") as temp:
                temp_path = Path(temp)
                zip_path = temp_path / "project.zip"
                await message.chat.send_action(ChatAction.TYPING)
                telegram_file = await document.get_file()
                await telegram_file.download_to_drive(zip_path)

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
                        preview_sent = await _send_preview(message, preview, job_id)
                        if not preview_sent:
                            await status.edit_text(
                                "اكتمل البناء، لكن تيليجرام لم يستقبل صورة المعاينة. "
                                "سأتابع الآن وأرسل ملف IPA."
                            )

                    if ipa.stat().st_size > SETTINGS.telegram_max_upload_mb * 1024 * 1024:
                        await status.edit_text(
                            f"اكتمل البناء، لكن حجم IPA أكبر من حد إرسال البوت "
                            f"({SETTINGS.telegram_max_upload_mb} MB). النتيجة: {run['html_url']}"
                        )
                        return

                    await message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)
                    await _send_ipa(message, ipa, job_id)
                    await status.edit_text("اكتمل البناء والإرسال بنجاح ✅")

        except (UnsafeArchive, GitHubError, OSError, ValueError, TimedOut, NetworkError) as exc:
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
    request = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=30,
        read_timeout=120,
        write_timeout=120,
        pool_timeout=30,
        media_write_timeout=300,
    )
    application = (
        Application.builder()
        .token(SETTINGS.telegram_token)
        .request(request)
        .get_updates_connect_timeout(30)
        .get_updates_read_timeout(60)
        .get_updates_write_timeout(60)
        .get_updates_pool_timeout(30)
        .build()
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("whoami", whoami))
    application.add_handler(MessageHandler(filters.Document.ALL, build_zip))
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
