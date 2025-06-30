import threading
from flask import Flask
import os
import logging
import sqlite3
import telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import shutil

# قراءة التوكن من Replit Secrets
TOKEN = os.environ.get("TELEGRAM_TOKEN")
DB_FILE = "bot_database.db"
FILES_DIR = "files" 

# معرف المستخدم الخاص بالـ Super Admin (استبدله بمعرفك الحقيقي)
SUPER_ADMIN_ID = 865863270 # تأكد من تحديث هذا المعرف إلى معرفك الحقيقي

# إعداد السجلات
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- وظائف قاعدة البيانات ---

def setup_database():
    """تنشئ قاعدة البيانات والجداول إذا لم تكن موجودة، وتنشئ مجلد الملفات."""
    if not os.path.exists(FILES_DIR):
        os.makedirs(FILES_DIR)
        logger.info(f"Created files directory: {FILES_DIR}")

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        role TEXT NOT NULL DEFAULT 'user',
        join_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_name TEXT NOT NULL,
        file_path TEXT NOT NULL UNIQUE,
        is_folder INTEGER NOT NULL DEFAULT 0, -- 0 for file, 1 for folder
        size_bytes INTEGER,
        uploaded_by INTEGER,
        upload_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    
    conn.commit()
    conn.close()
    logger.info("Database setup complete. `users` and `files` tables are ready.")


# --- وظائف مساعدة للتحقق من الصلاحيات ---

def get_user_role(user_id: int) -> str:
    """تجلب دور المستخدم من قاعدة البيانات."""
    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT role FROM users WHERE user_id = ?", (user_id,))
        result = cursor.fetchone()
        return result[0] if result else 'unregistered'
    except sqlite3.Error as e:
        logger.error(f"Database error getting user role: {e}")
        return 'error'
    finally:
        if conn:
            conn.close()

def is_super_admin(user_id: int) -> bool:
    """تتحقق مما إذا كان معرف المستخدم هو Super Admin."""
    return get_user_role(user_id) == 'super_admin'

def is_admin_or_higher(user_id: int) -> bool:
    """تتحقق مما إذا كان المستخدم أدمن أو Super Admin."""
    role = get_user_role(user_id)
    return role in ['admin', 'super_admin']

def is_uploader_or_higher(user_id: int) -> bool:
    """تتحقق مما إذا كان المستخدم رافعًا (uploader), أدمن, أو Super Admin."""
    role = get_user_role(user_id)
    return role in ['uploader', 'admin', 'super_admin']

# --- وظائف البوت الرئيسية (Handlers) ---

async def send_main_keyboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يرسل لوحة المفاتيح الرئيسية للمستخدم."""
    user_id = update.effective_user.id if update.effective_message else update.callback_query.from_user.id
    
    keyboard = [
        [InlineKeyboardButton("تصفح الملفات 📁", callback_data="ls_root")],
        [InlineKeyboardButton("دوري 👤", callback_data="my_role")],
        [InlineKeyboardButton("تواصل مع الإدارة 📧", callback_data="contact_admin_btn")],
    ]
    
    # إضافة زر "أوامر الإدارة" إذا كان المستخدم أدمن أو أعلى
    if is_admin_or_higher(user_id):
        keyboard.append([InlineKeyboardButton("أوامر الإدارة ⚙️", callback_data="admin_menu")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # استخدام update.effective_message للوصول إلى الرسالة سواء كانت أمر أو استدعاء زر
    try:
        if update.callback_query:
            await update.callback_query.edit_message_text("اختر أحد الخيارات:", reply_markup=reply_markup)
        else: # إذا كان من /start أو أمر آخر
            await update.message.reply_text("اختر أحد الخيارات:", reply_markup=reply_markup)
    except Exception as e:
        logger.warning(f"Failed to send/edit main keyboard: {e}")
        # fallback if edit fails (e.g., message not found)
        await update.effective_message.reply_text("اختر أحد الخيارات:", reply_markup=reply_markup)
        
async def show_folder_creation_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, current_path: str):
    """(دالة معدلة ومحصّنة) تستخدم المسارات النسبية لتجنب خطأ طول بيانات الزر."""
    logger.info(f"Showing folder creation menu for path: {current_path}")
    keyboard = []
    
    # --- تعديل لاستخدام المسارات النسبية ---
    root_abs_path = os.path.normpath(os.path.abspath(FILES_DIR))
    current_abs_path = os.path.normpath(os.path.abspath(current_path))
    
    # نحول المسار الحالي إلى مسار نسبي لبيانات الزر
    # إذا كنا في الجذر، المسار النسبي هو '.'
    current_rel_path = os.path.relpath(current_abs_path, root_abs_path)
    
    keyboard.append([InlineKeyboardButton("➕ إنشاء مجلد هنا", callback_data=f"create_here_{current_rel_path}")])

    # جلب وعرض المجلدات الفرعية
    subfolders = []
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT file_name, file_path FROM files WHERE is_folder = 1 ORDER BY file_name")
        all_folders_from_db = cursor.fetchall()

    for name, path in all_folders_from_db:
        try:
            folder_abs_path = os.path.normpath(os.path.abspath(path))
            if os.path.dirname(folder_abs_path) == current_abs_path:
                subfolders.append({'name': name, 'path': folder_abs_path})
        except Exception as e:
            logger.error(f"Error processing folder {name}: {str(e)}")

    for folder in subfolders:
        # نستخدم المسار النسبي للمجلد الفرعي في بيانات الزر
        folder_rel_path = os.path.relpath(folder['path'], root_abs_path)
        keyboard.append([InlineKeyboardButton(f"📂 {folder['name']}/", callback_data=f"nav_create_{folder_rel_path}")])

    # أزرار التنقل والعودة
    if current_abs_path != root_abs_path:
        parent_dir_abs = os.path.dirname(current_abs_path)
        # نستخدم المسار النسبي للمجلد الأصل في بيانات الزر
        parent_dir_rel = os.path.relpath(parent_dir_abs, root_abs_path)
        keyboard.append([InlineKeyboardButton("⬆️ عودة للمجلد الأعلى", callback_data=f"nav_create_{parent_dir_rel}")])
    
    keyboard.append([InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    dir_name = os.path.basename(current_path) if current_abs_path != root_abs_path else "الجذر"
    message_text = f"اختر مكان إنشاء المجلد الجديد، أو تنقل عبر المجلدات.\n\nالمسار الحالي: `{dir_name}`"

    try:
        if update.callback_query:
            await update.callback_query.edit_message_text(message_text, reply_markup=reply_markup, parse_mode='Markdown')
        else:
            await update.message.reply_text(message_text, reply_markup=reply_markup, parse_mode='Markdown')
    except telegram.error.BadRequest as e:
        if "Message is not modified" in str(e):
            logger.info("Ignoring 'Message is not modified' error.")
            if update.callback_query:
                await update.callback_query.answer()
        else:
            logger.error(f"An unexpected BadRequest occurred in show_folder_creation_menu: {e}")
            if update.callback_query:
                await update.callback_query.answer(f"حدث خطأ: {e}", show_alert=True)

async def send_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """(نسخة معدلة) يرسل لوحة المفاتيح الخاصة بأوامر الإدارة."""
    user_id = update.effective_user.id if update.effective_message else update.callback_query.from_user.id

    if not is_admin_or_higher(user_id):
        # ... (الكود هنا يبقى كما هو)
        return

    keyboard = [
        [InlineKeyboardButton("إنشاء مجلد جديد 📁", callback_data="admin_newfolder")],
        # --- تعديل: تغيير وظيفة زر الحذف ---
        [InlineKeyboardButton("حذف ملف/مجلد (تفاعلي) 🗑️", callback_data="admin_delete_start")],
        # ------------------------------------
        [InlineKeyboardButton("رفع ملف 📤", callback_data="admin_upload_info")],
        [InlineKeyboardButton("عرض الإحصائيات 📊", callback_data="admin_stats_button")],
    ]

    # ... (بقية الكود في الدالة يبقى كما هو)
    if is_super_admin(user_id):
        keyboard.append([InlineKeyboardButton("إدارة الأدوار 👥", callback_data="admin_roles_menu")])
        keyboard.append([InlineKeyboardButton("بث رسالة للمستخدمين 📢", callback_data="admin_broadcast_button")])
        keyboard.append([InlineKeyboardButton("قائمة الأدمنز والرافعين 📜", callback_data="admin_list_admins_button")])

    keyboard.append([InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    
    try:
        # تأكد من أننا نستخدم query عند الاستدعاء من زر
        message_to_edit = update.callback_query.message if update.callback_query else update.effective_message
        await message_to_edit.edit_text("اختر أمرًا إداريًا:", reply_markup=reply_markup)
    except Exception as e:
        logger.warning(f"Failed to edit message for admin menu, sending new one: {e}")
        await context.bot.send_message(chat_id=user_id, text="اختر أمرًا إداريًا:", reply_markup=reply_markup)

async def send_admin_roles_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يرسل لوحة المفاتيح الخاصة بإدارة الأدوار (للـ Super Admin)."""
    user_id = update.effective_user.id if update.effective_message else update.callback_query.from_user.id

    if not is_super_admin(user_id):
        await update.effective_message.reply_text("عذرًا، أنت لا تملك الصلاحية للوصول إلى هذه القائمة.")
        await send_main_keyboard(update.callback_query, context)
        return

    keyboard = [
        [InlineKeyboardButton("تعيين دور لمستخدم ➕", callback_data="admin_set_role")],
        [InlineKeyboardButton("إزالة دور أدمن/رافع ➖", callback_data="admin_remove_role")],
        [InlineKeyboardButton("⬅️ العودة لأوامر الإدارة", callback_data="admin_menu")],
        [InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await update.callback_query.edit_message_text("اختر إجراءً لإدارة الأدوار:", reply_markup=reply_markup)
    except Exception as e:
        logger.warning(f"Failed to edit message for admin roles menu, sending new one: {e}")
        await update.effective_message.reply_text("اختر إجراءً لإدارة الأدوار:", reply_markup=reply_markup)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """تسجل المستخدم عند البدء وترسل رسالة ترحيبية مع الأزرار الرئيسية."""
    user = update.effective_user
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        cursor.execute("SELECT role FROM users WHERE user_id = ?", (user.id,))
        existing_user = cursor.fetchone()

        if existing_user:
            cursor.execute(
                "UPDATE users SET username = ? WHERE user_id = ?",
                (user.username, user.id)
            )
            conn.commit()
            logger.info(f"User {user.username} (ID: {user.id}) updated.")
            await update.message.reply_text(f'أهلاً بك مرة أخرى يا {user.first_name}! تم تحديث بياناتك.')
        else:
            cursor.execute(
                "INSERT INTO users (user_id, username, role) VALUES (?, ?, ?)",
                (user.id, user.username, 'user')
            )
            logger.info(f"User {user.username} (ID: {user.id}) started the bot and was registered.")
            await update.message.reply_text(f'أهلاً بك يا {user.first_name}! تم تسجيلك بنجاح في قاعدة البيانات المحلية.')

            if user.id == SUPER_ADMIN_ID:
                cursor.execute(
                    "UPDATE users SET role = ? WHERE user_id = ?",
                    ('super_admin', SUPER_ADMIN_ID)
                )
                logger.info(f"User {user.username} (ID: {user.id}) has been set as Super Admin.")
                await update.message.reply_text("تم تعيينك كمدير أعلى (Super Admin)!")
        
        conn.commit()
        
    except sqlite3.Error as e:
        logger.error(f"Database error on start: {e}")
        await update.message.reply_text("حدث خطأ في قاعدة البيانات.")
    finally:
        if conn:
            conn.close()
    
    await send_main_keyboard(update, context)
    
async def text_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """معالج ذكي للرسائل النصية، يتصرف بناءً على حالة المستخدم."""
    user_action = context.user_data.get('user_action')

    # التحقق: هل ينتظر البوت اسم مجلد جديد من هذا المستخدم؟
    if user_action == 'awaiting_new_folder_name':
        await handle_new_folder_creation(update, context)
    
    # يمكنك إضافة حالات أخرى هنا في المستقبل ...
    # elif user_action == 'some_other_action':
    #     await handle_some_other_action(update, context)
    
    else:
        # إذا كانت رسالة نصية عادية ولا توجد حالة خاصة، يمكن تجاهلها
        # أو الرد برسالة افتراضية
        logger.info(f"Received a generic text message from {update.effective_user.id}, ignoring.")
        # await update.message.reply_text("أمر غير معروف. استخدم /start للقائمة.")

async def my_role(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يتحقق من دور المستخدم ويرسل له رسالة (معالج أمر أو زر)."""
    user_id = update.effective_user.id if update.effective_message else update.callback_query.from_user.id
    
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        cursor.execute("SELECT role FROM users WHERE user_id = ?", (user_id,))
        result = cursor.fetchone()
        
        response_text = ""
        if result:
            role = result[0]
            response_text = f"دورك المسجل هو: {role}"
        else:
            response_text = "أنت غير مسجل بعد. الرجاء إرسال /start أولاً."
            
        # إضافة زر للعودة للقائمة الرئيسية
        keyboard = [[InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        if update.callback_query:
            await update.callback_query.edit_message_text(response_text, reply_markup=reply_markup)
        else:
            await update.message.reply_text(response_text, reply_markup=reply_markup)
            
    except sqlite3.Error as e:
        logger.error(f"Database error on my_role: {e}")
        if update.callback_query:
            await update.callback_query.edit_message_text("حدث خطأ في قاعدة البيانات.")
        else:
            await update.message.reply_text("حدث خطأ في قاعدة البيانات.")
    finally:
        if conn:
            conn.close()

async def contact_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يرسل رسالة من المستخدم إلى جميع الأدمنز. الاستخدام: /contact_admin <رسالتك>"""
    user = update.effective_user
    args = context.args
    
    if not args:
        if update.callback_query:
            await update.callback_query.edit_message_text("للتواصل مع الإدارة، الرجاء استخدام الأمر النصي: `/contact_admin <رسالتك>`")
            # يمكن إضافة زر للعودة هنا
            await send_main_keyboard(update.callback_query, context)
            return
        await update.message.reply_text("الرجاء كتابة رسالتك بعد الأمر. مثال: /contact_admin الملف الفلاني لا يعمل.")
        return

    message_text = " ".join(args)
    
    admin_users = []
    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM users WHERE role IN ('admin', 'super_admin')")
        admin_ids = cursor.fetchall()
        
        for admin_id_tuple in admin_ids:
            admin_users.append(admin_id_tuple[0])
            
        if not admin_users:
            response_text = "عذرًا، لا يوجد أدمنز مسجلون حاليًا لتلقي رسالتك."
            logger.warning(f"User {user.username} tried to contact admin, but no admins found.")
            if update.callback_query:
                await update.callback_query.edit_message_text(response_text)
            else:
                await update.message.reply_text(response_text)
            return

        full_message = (
            f"رسالة من المستخدم @{user.username} (ID: {user.id}, الدور: {get_user_role(user.id)}):\n\n"
            f"{message_text}"
        )
        
        for admin_id in admin_users:
            try:
                await context.bot.send_message(chat_id=admin_id, text=full_message)
            except Exception as e:
                logger.error(f"Failed to send message to admin ID {admin_id}: {e}")
        
        response_text = "تم إرسال رسالتك إلى الإدارة بنجاح."
        logger.info(f"User {user.username} (ID: {user.id}) sent a message to admins.")
        
        if update.callback_query:
            await update.callback_query.edit_message_text(response_text)
        else:
            await update.message.reply_text(response_text)

    except sqlite3.Error as e:
        logger.error(f"Database error on contact_admin: {e}")
        if update.callback_query:
            await update.callback_query.edit_message_text("حدث خطأ في قاعدة البيانات أثناء محاولة إرسال رسالتك.")
        else:
            await update.message.reply_text("حدث خطأ في قاعدة البيانات أثناء محاولة إرسال رسالتك.")
    except Exception as e:
        logger.error(f"General error on contact_admin: {e}")
        if update.callback_query:
            await update.callback_query.edit_message_text("حدث خطأ عام أثناء محاولة إرسال رسالتك.")
        else:
            await update.message.reply_text("حدث خطأ عام أثناء محاولة إرسال رسالتك.")
    finally:
        if conn:
            conn.close()

async def new_folder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """(النسخة الجديدة) تبدأ واجهة إنشاء المجلدات التفاعلية."""
    user_id = update.effective_user.id
    if not is_admin_or_higher(user_id):
        await update.message.reply_text("عذرًا، أنت لا تملك الصلاحية لإنشاء مجلدات جديدة.")
        return

    # استدعاء دالة عرض القائمة مباشرة، بدءًا من الجذر
    await show_folder_creation_menu(update, context, os.path.abspath(FILES_DIR))
    
    
async def handle_new_folder_creation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """(نسخة محسنة ومصححة) تأخذ اسم المجلد، تنشئه، ثم تعرض المجلد الأب المحدث."""
    user_id = update.effective_user.id
    folder_name = update.message.text
    parent_path = context.user_data.get('creation_path', FILES_DIR)

    if ".." in folder_name or "/" in folder_name or "\\" in folder_name:
        await update.message.reply_text("اسم المجلد يحتوي على أحرف غير مسموح بها. تم إلغاء العملية.")
        context.user_data.pop('user_action', None)
        return

    full_path = os.path.abspath(os.path.join(parent_path, folder_name))

    try:
        if os.path.exists(full_path):
            await update.message.reply_text(f"المجلد '{folder_name}' موجود بالفعل. تم إلغاء العملية.")
            logger.warning(f"User {user_id} attempted to create existing folder via text: {folder_name}")
        else:
            # إنشاء المجلد في نظام الملفات وفي قاعدة البيانات
            os.makedirs(full_path)
            with sqlite3.connect(DB_FILE) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO files (file_name, file_path, is_folder, uploaded_by) VALUES (?, ?, ?, ?)",
                    (folder_name, full_path, 1, user_id)
                )
                conn.commit()
            
            # إرسال رسالة تأكيد سريعة
            await update.message.reply_text(f"✅ تم إنشاء المجلد '{folder_name}' بنجاح.")
            logger.info(f"User {user_id} created folder '{folder_name}' via text.")

            # --- الجزء الجديد والمهم (التصحيح) ---
            # أولاً، نرسل رسالة مؤقتة يمكننا تعديلها لاحقًا
            status_message = await update.message.reply_text("جاري تحديث القائمة...")
            
            # ثانيًا، نستدعي دالة العرض لتعديل الرسالة المؤقتة التي أرسلناها للتو
            await list_files_with_buttons(status_message, context, parent_path)
            # --- نهاية الجزء الجديد ---

    except Exception as e:
        logger.error(f"Error during folder creation from text: {e}")
        await update.message.reply_text("حدث خطأ أثناء إنشاء المجلد.")
    finally:
        # تنظيف الحالة بعد انتهاء العملية
        context.user_data.pop('user_action', None)
        context.user_data.pop('creation_path', None)

async def handle_media_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    (نسخة جديدة ومحسّنة) يلتقط أي ملف يتم إرساله ويبدأ عملية اختيار وجهة الحفظ.
    """
    user_id = update.effective_user.id
    user_username = update.effective_user.username

    # 1. التحقق من صلاحية الرفع
    if not is_uploader_or_higher(user_id):
        logger.warning(f"User {user_username} (ID: {user_id}) with role '{get_user_role(user_id)}' attempted to upload a file without permission.")
        # يمكن إرسال رسالة أو تجاهل الطلب بصمت
        return

    # 2. استخلاص معلومات الملف
    file_to_process = update.message.document or update.message.photo[-1] or update.message.video
    if not file_to_process:
        return # لا يوجد ملف صالح للمعالجة

    file_id = file_to_process.file_id
    file_size = file_to_process.file_size
    # الحصول على اسم الملف أو إنشاء اسم فريد للصور التي ليس لها اسم
    file_name = getattr(file_to_process, 'file_name', f"{file_to_process.file_unique_id}.jpg")

    # 3. حفظ معلومات الملف مؤقتاً في سياق المستخدم
    context.user_data['pending_upload'] = {
        'file_id': file_id,
        'file_name': file_name,
        'file_size': file_size,
    }
    logger.info(f"User {user_username} initiated an upload for file: {file_name}. Awaiting destination.")
    
   
    # ...
    # 4. عرض قائمة المجلدات للمستخدم ليختار منها، بدءاً من الجذر
    await show_upload_destination_menu(update, context, os.path.abspath(FILES_DIR))



async def show_upload_destination_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, current_path: str):
    """
    (نسخة محسّنة)
    يعرض واجهة تصفح هرمية لاختيار مجلد وجهة الرفع،
    مع منع الرفع في المجلد الجذري.
    """
    keyboard = []
    
    root_abs_path = os.path.normpath(os.path.abspath(FILES_DIR))
    current_abs_path = os.path.normpath(os.path.abspath(current_path))
    
    # --- <<< التعديل الرئيسي هنا >>> ---
    # 1. زر لتحديد المجلد الحالي كوجهة (يظهر فقط إذا لم نكن في المجلد الجذري)
    if current_abs_path != root_abs_path:
        current_rel_path = os.path.relpath(current_abs_path, root_abs_path)
        keyboard.append([InlineKeyboardButton("✅ حدد هذا المجلد للحفظ هنا", callback_data=f"upload_to_{current_rel_path}")])

    # 2. جلب وعرض المجلدات الفرعية للتنقل (لا تغيير هنا)
    subfolders = []
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT file_name, file_path FROM files WHERE is_folder = 1 ORDER BY file_name")
            all_folders = cursor.fetchall()

        for name, path in all_folders:
            folder_abs_path = os.path.normpath(os.path.abspath(path))
            if os.path.dirname(folder_abs_path) == current_abs_path:
                subfolders.append({'name': name, 'path': folder_abs_path})

        for folder in subfolders:
            folder_rel_path = os.path.relpath(folder['path'], root_abs_path)
            keyboard.append([InlineKeyboardButton(f"📂 {folder['name']}/", callback_data=f"nav_upload_{folder_rel_path}")])
    
    except sqlite3.Error as e:
        logger.error(f"DB error in show_upload_destination_menu: {e}")

    # 3. زر العودة للمجلد الأعلى (لا تغيير هنا)
    if current_abs_path != root_abs_path:
        parent_dir_abs = os.path.dirname(current_abs_path)
        parent_dir_rel = os.path.relpath(parent_dir_abs, root_abs_path)
        keyboard.append([InlineKeyboardButton("⬆️ عودة للمجلد الأعلى", callback_data=f"nav_upload_{parent_dir_rel}")])
    
    # 4. زر إلغاء العملية بالكامل (لا تغيير هنا)
    keyboard.append([InlineKeyboardButton("❌ إلغاء الرفع", callback_data="cancel_upload")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    # --- <<< تعديل نص الرسالة ليكون أكثر وضوحاً >>> ---
    file_info = context.user_data.get('pending_upload', {})
    file_name_str = f"للملف: `{file_info.get('file_name', 'غير معروف')}`"
    dir_name = os.path.basename(current_path) if current_abs_path != root_abs_path else "الجذر"
    
    # رسالة مخصصة للمجلد الجذري
    if current_abs_path == root_abs_path:
        message_text = f"اختر مجلداً لحفظ الملف فيه {file_name_str}\n\n*ملاحظة: لا يمكن الحفظ في المجلد الجذري مباشرة، يجب الدخول إلى مجلد فرعي أولاً.*"
    else: # الرسالة العادية للمجلدات الفرعية
        message_text = f"اختر وجهة الحفظ {file_name_str}\n\nالمسار الحالي: `{dir_name}`"

    # تحديد هل نرسل رسالة جديدة أم نعدل رسالة موجودة
    if update.callback_query:
        await update.callback_query.edit_message_text(message_text, reply_markup=reply_markup, parse_mode='Markdown')
    else:
        await update.message.reply_text(message_text, reply_markup=reply_markup, parse_mode='Markdown')
        
        
        
async def delete_item_logic(item_path_to_delete: str) -> (bool, str):
    """
    (جديد) دالة منطقية لتنفيذ عملية الحذف من نظام الملفات وقاعدة البيانات.
    ترجع (True, "رسالة نجاح") أو (False, "رسالة خطأ").
    """
    item_abs_path = os.path.abspath(item_path_to_delete)
    item_name = os.path.basename(item_abs_path)
    
    # حماية أمنية: التأكد من أن المسار داخل مجلد الملفات الرئيسي
    if not item_abs_path.startswith(os.path.abspath(FILES_DIR)):
        error_msg = f"Security alert: Attempted to delete path outside FILES_DIR: {item_abs_path}"
        logger.critical(error_msg)
        return False, "خطأ أمني: المسار غير صالح."

    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            # نستخدم المسار الكامل للبحث لضمان الدقة
            cursor.execute("SELECT is_folder FROM files WHERE file_path = ?", (item_abs_path,))
            result = cursor.fetchone()

            if not result:
                return False, f"العنصر '{item_name}' غير موجود في قاعدة البيانات."

            is_folder = bool(result[0])

            # الحذف من نظام الملفات
            if os.path.exists(item_abs_path):
                if is_folder:
                    shutil.rmtree(item_abs_path)
                else:
                    os.remove(item_abs_path)
            
            # الحذف من قاعدة البيانات
            cursor.execute("DELETE FROM files WHERE file_path = ?", (item_abs_path,))
            # إذا كان مجلداً، نحذف كل الملفات الفرعية من قاعدة البيانات أيضاً
            if is_folder:
                cursor.execute("DELETE FROM files WHERE file_path LIKE ?", (item_abs_path + '%',))
            
            conn.commit()
            success_msg = f"تم حذف '{item_name}' بنجاح."
            logger.info(success_msg)
            return True, success_msg

    except Exception as e:
        error_msg = f"Error during deletion of {item_abs_path}: {e}"
        logger.error(error_msg)
        return False, "حدث خطأ فادح أثناء عملية الحذف."

async def show_deletion_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, current_path: str):
    """
    (جديد) يعرض واجهة تفاعلية لاختيار وحذف الملفات والمجلدات.
    """
    query = update.callback_query
    logger.info(f"Admin {query.from_user.username} is Browse for deletion at: {current_path}")
    keyboard = []

    root_abs_path = os.path.normpath(os.path.abspath(FILES_DIR))
    current_abs_path = os.path.normpath(os.path.abspath(current_path))
    
    items_in_current_dir = []
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT file_name, file_path, is_folder FROM files ORDER BY is_folder DESC, file_name ASC")
            all_items = cursor.fetchall()

        for name, path, is_folder in all_items:
            item_abs_path = os.path.normpath(os.path.abspath(path))
            if os.path.dirname(item_abs_path) == current_abs_path:
                items_in_current_dir.append({'name': name, 'path': item_abs_path, 'is_folder': bool(is_folder)})
        
        # إنشاء الأزرار
        for item in items_in_current_dir:
            item_rel_path = os.path.relpath(item['path'], root_abs_path)
            icon = "📁" if item['is_folder'] else "📄"
            # زر التنقل (للمجلدات) وزر الحذف
            nav_button = InlineKeyboardButton(f"{icon} {item['name']}", callback_data=f"nav_delete_{item_rel_path}" if item['is_folder'] else "noop") # noop = no operation
            delete_button = InlineKeyboardButton("🗑️", callback_data=f"confirm_delete_{item_rel_path}")
            keyboard.append([nav_button, delete_button])

    except Exception as e:
        logger.error(f"Error building deletion menu: {e}")

    # أزرار التنقل والعودة
    if current_abs_path != root_abs_path:
        parent_dir_abs = os.path.dirname(current_abs_path)
        parent_dir_rel = os.path.relpath(parent_dir_abs, root_abs_path)
        keyboard.append([InlineKeyboardButton("⬆️ عودة للمجلد الأعلى", callback_data=f"nav_delete_{parent_dir_rel}")])
    
    keyboard.append([InlineKeyboardButton("⬅️ العودة لقائمة الإدارة", callback_data="admin_menu")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    dir_name = os.path.basename(current_path) if current_abs_path != root_abs_path else "الجذر"
    message_text = f"اختر عنصراً لحذفه، أو تصفح المجلدات.\n\nالمسار الحالي: `{dir_name}`"
    if not items_in_current_dir:
        message_text = f"المجلد *'{dir_name}'* فارغ.\n\nاضغط للعودة."

    await query.edit_message_text(message_text, reply_markup=reply_markup, parse_mode='Markdown')
    

async def delete_item(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يحذف ملفًا أو مجلدًا. يتطلب دور 'admin' أو 'super_admin'. الاستخدام: /delete <اسم_الشيء_للحذف>"""
    user_id = update.effective_user.id
    user_username = update.effective_user.username

    if not is_admin_or_higher(user_id):
        await update.message.reply_text("عذرًا، أنت لا تملك الصلاحية لحذف الملفات أو المجلدات.")
        logger.warning(f"User {user_username} (ID: {user_id}) attempted to delete without permission.")
        return

    args = context.args
    if not args:
        await update.message.reply_text("الرجاء تحديد اسم الملف أو المجلد للحذف. الاستخدام: /delete <اسم_الشيء>")
        return

    item_name_to_delete = " ".join(args)
    
    if ".." in item_name_to_delete or "/" in item_name_to_delete or "\\" in item_name_to_delete:
        await update.message.reply_text("اسم الملف أو المجلد يحتوي على أحرف غير مسموح بها لأسباب أمنية.")
        logger.warning(f"Admin {user_username} attempted unsafe delete path: {item_name_to_delete}")
        return

    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()

        cursor.execute("SELECT file_path, is_folder FROM files WHERE file_name = ?", (item_name_to_delete,))
        result = cursor.fetchone()

        if not result:
            await update.message.reply_text(f"العنصر '{item_name_to_delete}' غير موجود في قاعدة البيانات.")
            logger.info(f"Admin {user_username} attempted to delete non-existent item: {item_name_to_delete}")
            return

        file_path_from_db = result[0]
        is_folder = bool(result[1])

        if not os.path.realpath(file_path_from_db).startswith(os.path.realpath(FILES_DIR)):
            await update.message.reply_text("خطأ أمني: محاولة حذف مسار غير صالح.")
            logger.critical(f"Security alert: Admin {user_username} attempted to delete path outside FILES_DIR: {file_path_from_db}")
            return

        if os.path.exists(file_path_from_db):
            if is_folder:
                shutil.rmtree(file_path_from_db)
                logger.info(f"Admin {user_username} deleted folder: {file_path_from_db}")
            else:
                os.remove(file_path_from_db)
                logger.info(f"Admin {user_username} deleted file: {file_path_from_db}")
            
            cursor.execute("DELETE FROM files WHERE file_name = ?", (item_name_to_delete,))
            conn.commit()
            await update.message.reply_text(f"تم حذف '{item_name_to_delete}' بنجاح.")
        else:
            await update.message.reply_text(f"العنصر '{item_name_to_delete}' غير موجود فعليًا في نظام الملفات، ولكنه موجود في قاعدة البيانات. تم إزالته من قاعدة البيانات.")
            cursor.execute("DELETE FROM files WHERE file_name = ?", (item_name_to_delete,)) 
            conn.commit()
            logger.warning(f"Admin {user_username} deleted database entry for non-existent file: {item_name_to_delete}")

    except sqlite3.Error as e:
        logger.error(f"Database error on delete_item: {e}")
        await update.message.reply_text("حدث خطأ في قاعدة البيانات أثناء عملية الحذف.")
    except OSError as e:
        logger.error(f"File system error on delete_item {item_name_to_delete}: {e}")
        await update.message.reply_text(f"حدث خطأ في نظام الملفات أثناء حذف '{item_name_to_delete}'. ربما المجلد غير فارغ ولا يمكن حذفه مباشرة إذا كان ملفًا.")
    finally:
        if conn:
            conn.close()

async def add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يضيف مستخدمًا كأدمن أو يقوم بتغيير دوره. الاستخدام: /addadmin @username [role]"""
    if not is_super_admin(update.effective_user.id):
        await update.message.reply_text("عذرًا، أنت لا تملك الصلاحية لتنفيذ هذا الأمر.")
        return

    args = context.args
    if not args or len(args) < 1 or not args[0].startswith('@'):
        await update.message.reply_text("الاستخدام الصحيح: /addadmin @username [role] (الأدوار المتاحة: admin, uploader, user)")
        return

    target_username = args[0].lstrip('@')
    target_role = args[1].lower() if len(args) > 1 else 'admin' 
    
    allowed_roles = ['admin', 'uploader', 'user']
    if target_role not in allowed_roles:
        await update.message.reply_text(f"دور غير صالح: {target_role}. الأدوار المتاحة: {', '.join(allowed_roles)}")
        return

    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()

        cursor.execute("SELECT user_id FROM users WHERE username = ?", (target_username,))
        result = cursor.fetchone()

        if result:
            target_user_id = result[0]
            cursor.execute(
                "UPDATE users SET role = ? WHERE user_id = ?",
                (target_role, target_user_id)
            )
            conn.commit()
            await update.message.reply_text(f"تم تحديث دور المستخدم @{target_username} إلى: {target_role}")
            logger.info(f"Super Admin {update.effective_user.username} changed role of {target_username} to {target_role}")
        else:
            await update.message.reply_text(f"المستخدم @{target_username} غير مسجل في البوت. اطلب منه أن يرسل /start أولاً.")
            logger.warning(f"Attempt to add non-registered user {target_username} by Super Admin {update.effective_user.username}")

    except sqlite3.Error as e:
        logger.error(f"Database error on add_admin: {e}")
        await update.message.reply_text("حدث خطأ في قاعدة البيانات أثناء إضافة الأدمن.")
    finally:
        if conn:
            conn.close()

async def remove_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يزيل دور أدمن أو يعيد تعيينه كـ user عادي. الاستخدام: /removeadmin @username"""
    if not is_super_admin(update.effective_user.id):
        await update.message.reply_text("عذرًا، أنت لا تملك الصلاحية لتنفيذ هذا الأمر.")
        return

    args = context.args
    if not args or len(args) < 1 or not args[0].startswith('@'):
        await update.message.reply_text("الاستخدام الصحيح: /removeadmin @username")
        return

    target_username = args[0].lstrip('@')

    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()

        cursor.execute("SELECT user_id, role FROM users WHERE username = ?", (target_username,))
        result = cursor.fetchone()

        if result:
            target_user_id, current_role = result[0], result[1]
            if current_role == 'super_admin' and target_user_id != SUPER_ADMIN_ID:
                await update.message.reply_text("لا يمكنك إزالة دور Super Admin الرئيسي.")
                return

            cursor.execute(
                "UPDATE users SET role = ? WHERE user_id = ?",
                ('user', target_user_id)
            )
            conn.commit()
            await update.message.reply_text(f"تم إعادة تعيين دور المستخدم @{target_username} إلى: user (مستخدم عادي).")
            logger.info(f"Super Admin {update.effective_user.username} removed admin role from {target_username}")
        else:
            await update.message.reply_text(f"المستخدم @{target_username} غير مسجل في البوت.")
            logger.warning(f"Attempt to remove non-registered user {target_username} by Super Admin {update.effective_user.username}")

    except sqlite3.Error as e:
        logger.error(f"Database error on remove_admin: {e}")
        await update.message.reply_text("حدث خطأ في قاعدة البيانات أثناء إزالة الأدمن.")
    finally:
        if conn:
            conn.close()

# تم تعديل list_admins لتعمل مع الأزرار
async def list_admins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يعرض قائمة بجميع المستخدمين الذين لديهم أدوار إدارية (admin, uploader, super_admin)."""
    # هذا الأمر لا يزال موجودًا كـ CommandHandler ولكن يمكن استدعاؤه من الزر الآن
    user_id = update.effective_user.id 

    if not is_super_admin(user_id):
        await update.message.reply_text("عذرًا، أنت لا تملك الصلاحية لتنفيذ هذا الأمر.")
        return

    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()

        cursor.execute("SELECT username, role FROM users WHERE role != 'user' ORDER BY role")
        results = cursor.fetchall()

        response_message = ""
        if results:
            response_message = "قائمة الأدمنز والرافعين:\n"
            for username, role in results:
                response_message += f"- @{username} (الدور: {role})\n"
            logger.info(f"Super Admin {update.effective_user.username} listed admins.")
        else:
            response_message = "لا يوجد أدمنز أو رافعون مسجلون حاليًا."
        
        await update.message.reply_text(response_message)

    except sqlite3.Error as e:
        logger.error(f"Database error on list_admins: {e}")
        await update.message.reply_text("حدث خطأ في قاعدة البيانات أثناء جلب قائمة الأدمنز.")
    finally:
        if conn:
            conn.close()

# دالة مخصصة لاستدعاء قائمة الأدمنز من زر
async def list_admins_from_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يعرض قائمة بجميع المستخدمين الذين لديهم أدوار إدارية (admin, uploader, super_admin) من زر."""
    user_id = update.effective_user.id if update.effective_message else update.callback_query.from_user.id
    query = update.callback_query

    if not is_super_admin(user_id):
        await query.edit_message_text("عذرًا، أنت لا تملك الصلاحية لتنفيذ هذا الأمر.")
        await send_main_keyboard(update.callback_query, context)
        return

    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()

        cursor.execute("SELECT username, role FROM users WHERE role != 'user' ORDER BY role")
        results = cursor.fetchall()

        response_message = ""
        if results:
            response_message = "قائمة الأدمنز والرافعين:\n"
            for username, role in results:
                response_message += f"- @{username} (الدور: {role})\n"
            logger.info(f"Super Admin {update.effective_user.username} listed admins via button.")
        else:
            response_message = "لا يوجد أدمنز أو رافعون مسجلون حاليًا."
        
        keyboard = [[InlineKeyboardButton("⬅️ العودة لأوامر الإدارة", callback_data="admin_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(response_message, reply_markup=reply_markup, parse_mode='Markdown')

    except sqlite3.Error as e:
        logger.error(f"Database error on list_admins_from_button: {e}")
        await query.edit_message_text("حدث خطأ في قاعدة البيانات أثناء جلب قائمة الأدمنز.")
    finally:
        if conn:
            conn.close()

async def broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يرسل رسالة جماعية لجميع المستخدمين. يتطلب دور 'super_admin'. الاستخدام: /broadcast <رسالة_الإعلان>"""
    user = update.effective_user
    if not is_super_admin(user.id):
        await update.message.reply_text("عذرًا، أنت لا تملك الصلاحية لإرسال رسائل جماعية.")
        logger.warning(f"User {user.username} (ID: {user.id}) attempted unauthorized broadcast.")
        return

    args = context.args
    if not args:
        await update.message.reply_text("الرجاء كتابة الرسالة التي تود بثها. مثال: /broadcast رسالة مهمة للجميع.")
        return

    message_to_send = " ".join(args)
    
    all_user_ids = []
    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM users")
        user_ids_tuples = cursor.fetchall()
        
        for uid_tuple in user_ids_tuples:
            all_user_ids.append(uid_tuple[0])

        if not all_user_ids:
            await update.message.reply_text("لا يوجد مستخدمون مسجلون لبث الرسالة إليهم.")
            logger.warning(f"Super Admin {user.username} attempted broadcast, but no users found.")
            return

        success_count = 0
        fail_count = 0
        
        for target_user_id in all_user_ids:
            try:
                if target_user_id != user.id: 
                    await context.bot.send_message(chat_id=target_user_id, text=f"رسالة من الإدارة:\n\n{message_to_send}")
                    success_count += 1
            except Exception as e:
                logger.warning(f"Failed to send broadcast message to user ID {target_user_id}: {e}")
                fail_count += 1
        
        await update.message.reply_text(f"تم بث الرسالة بنجاح إلى {success_count} مستخدم. فشل الإرسال إلى {fail_count} مستخدم (ربما قاموا بحظر البوت).")
        logger.info(f"Super Admin {user.username} (ID: {user.id}) broadcasted a message to {success_count} users.")

    except sqlite3.Error as e:
        logger.error(f"Database error on broadcast_message: {e}")
        await update.message.reply_text("حدث خطأ في قاعدة البيانات أثناء محاولة بث الرسالة.")
    except Exception as e:
        logger.error(f"General error on broadcast_message: {e}")
        await update.message.reply_text("حدث خطأ عام أثناء محاولة بث الرسالة.")
    finally:
        if conn:
            conn.close()

# تم تعديل show_stats لتعمل مع الأزرار
async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يعرض إحصائيات حول البوت (عدد المستخدمين، الملفات، الحجم). يتطلب دور 'admin' أو 'super_admin'."""
    # هذا الأمر لا يزال موجودًا كـ CommandHandler ولكن يمكن استدعاؤه من الزر الآن
    user_id = update.effective_user.id
    if not is_admin_or_higher(user_id):
        await update.message.reply_text("عذرًا، أنت لا تملك الصلاحية لعرض الإحصائيات.")
        return

    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM users")
        total_users = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM files WHERE is_folder = 0")
        total_files = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM files WHERE is_folder = 1")
        total_folders = cursor.fetchone()[0]

        cursor.execute("SELECT SUM(size_bytes) FROM files WHERE is_folder = 0")
        total_size_bytes = cursor.fetchone()[0] or 0 

        total_size_mb = total_size_bytes / (1024 * 1024) if total_size_bytes else 0

        stats_message = (
            "📊 *إحصائيات البوت:*\n\n"
            f"👤 *إجمالي المستخدمين*: {total_users}\n"
            f"📄 *إجمالي الملفات*: {total_files}\n"
            f"📁 *إجمالي المجلدات*: {total_folders}\n"
            f"📦 *إجمالي حجم الملفات*: {total_size_mb:.2f} MB"
        )
        
        await update.message.reply_text(stats_message, parse_mode='Markdown')
        logger.info(f"Admin {update.effective_user.username} (ID: {user_id}) requested stats.")

    except sqlite3.Error as e:
        logger.error(f"Database error on show_stats: {e}")
        await update.message.reply_text("حدث خطأ في قاعدة البيانات أثناء جلب الإحصائيات.")
    finally:
        if conn:
            conn.close()

# دالة مخصصة لاستدعاء الإحصائيات من زر
async def show_stats_from_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يعرض إحصائيات حول البوت (عدد المستخدمين، الملفات، الحجم) من زر."""
    user_id = update.effective_user.id if update.effective_message else update.callback_query.from_user.id
    query = update.callback_query

    if not is_admin_or_higher(user_id):
        await query.edit_message_text("عذرًا، أنت لا تملك الصلاحية لعرض الإحصائيات.")
        await send_main_keyboard(update.callback_query, context)
        return

    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM users")
        total_users = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM files WHERE is_folder = 0")
        total_files = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM files WHERE is_folder = 1")
        total_folders = cursor.fetchone()[0]

        cursor.execute("SELECT SUM(size_bytes) FROM files WHERE is_folder = 0")
        total_size_bytes = cursor.fetchone()[0] or 0 

        total_size_mb = total_size_bytes / (1024 * 1024) if total_size_bytes else 0

        stats_message = (
            "📊 *إحصائيات البوت:*\n\n"
            f"👤 *إجمالي المستخدمين*: {total_users}\n"
            f"📄 *إجمالي الملفات*: {total_files}\n"
            f"📁 *إجمالي المجلدات*: {total_folders}\n"
            f"📦 *إجمالي حجم الملفات*: {total_size_mb:.2f} MB"
        )
        
        keyboard = [[InlineKeyboardButton("⬅️ العودة لأوامر الإدارة", callback_data="admin_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(stats_message, reply_markup=reply_markup, parse_mode='Markdown')
        logger.info(f"Admin {update.effective_user.username} (ID: {user_id}) requested stats via button.")

    except sqlite3.Error as e:
        logger.error(f"Database error on show_stats_from_button: {e}")
        await query.edit_message_text("حدث خطأ في قاعدة البيانات أثناء جلب الإحصائيات.")
    finally:
        if conn:
            conn.close()


async def list_files_with_buttons(message: 'telegram.Message', context: ContextTypes.DEFAULT_TYPE, current_dir: str) -> None:
    """(نسخة محدثة) يعرض الملفات مع زر تنزيل يستخدم المسار النسبي الدقيق."""
    user_id = message.chat_id
    current_path_key = f"{user_id}_current_path"
    context.user_data[current_path_key] = current_dir
    
    logger.info(f"User {user_id} navigating to: {current_dir}")

    keyboard = []
    items_in_current_dir = []
    root_abs_path = os.path.abspath(FILES_DIR)

    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT file_name, file_path, is_folder FROM files ORDER BY is_folder DESC, file_name ASC")
            all_items_db = cursor.fetchall()

        current_abs_path = os.path.abspath(current_dir)
        
        for name, path, is_folder in all_items_db:
            item_abs_path = os.path.abspath(path)
            if os.path.dirname(item_abs_path) == current_abs_path:
                items_in_current_dir.append({"name": name, "is_folder": bool(is_folder), "path": item_abs_path})
        
        for item in items_in_current_dir:
            if item['is_folder']:
                keyboard.append([InlineKeyboardButton(f"📁 {item['name']}/", callback_data=f"ls_{item['name']}")])
            else:
                # --- <<< التعديل الرئيسي هنا >>> ---
                # الآن زر التنزيل يحمل المسار النسبي للملف لضمان الدقة
                item_rel_path = os.path.relpath(item['path'], root_abs_path)
                keyboard.append([InlineKeyboardButton(f"📄 {item['name']}", callback_data=f"download_{item_rel_path}")])
                # --- نهاية التعديل ---
        
        if current_abs_path != root_abs_path:
            parent_dir = os.path.dirname(current_abs_path)
            display_parent_name = 'الجذر' if parent_dir == root_abs_path else os.path.basename(parent_dir)
            keyboard.append([InlineKeyboardButton(f"⬆️ العودة ({display_parent_name})", callback_data=f"ls_..")])
        
        keyboard.append([InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        current_display_name = 'الجذر' if current_abs_path == root_abs_path else os.path.basename(current_abs_path)
        response_text = f"محتويات المجلد: *{current_display_name}*"
        
        if not items_in_current_dir:
            response_text = f"المجلد *'{current_display_name}'* فارغ."
        
        await message.edit_text(response_text, reply_markup=reply_markup, parse_mode='Markdown')

    except Exception as e:
        logger.error(f"Error in list_files_with_buttons for path {current_dir}: {e}")
        try:
            await message.edit_text("حدث خطأ أثناء عرض محتويات المجلد.")
        except Exception as inner_e:
            logger.error(f"Could not even send error message: {inner_e}")
            
            
async def download_file_from_button(query: 'telegram.CallbackQuery', context: ContextTypes.DEFAULT_TYPE, relative_path: str) -> None:
    """(نسخة جديدة) يرسل ملفًا للمستخدم بناءً على المسار النسبي الدقيق من الزر."""
    user_username = query.from_user.username
    root_abs_path = os.path.abspath(FILES_DIR)
    
    # بناء المسار المطلق من المسار النسبي المستلم
    file_abs_path = os.path.abspath(os.path.join(root_abs_path, relative_path))

    # حماية أمنية
    if not file_abs_path.startswith(root_abs_path):
        await query.answer("خطأ أمني: مسار غير صالح.", show_alert=True)
        logger.critical(f"Security alert: User {user_username} attempted to download file outside FILES_DIR: {file_abs_path}")
        return

    try:
        if os.path.exists(file_abs_path) and os.path.isfile(file_abs_path):
            # نرسل الملف كـ "مستند" للحفاظ على نوعه الأصلي
            await context.bot.send_document(chat_id=query.from_user.id, document=open(file_abs_path, 'rb'))
            await query.answer(f"جاري إرسال الملف: {os.path.basename(file_abs_path)}")
            logger.info(f"User {user_username} downloaded file: {file_abs_path}")
        else:
            await query.answer("خطأ: الملف لم يعد موجوداً على الخادم.", show_alert=True)
            logger.warning(f"User {user_username} attempted to download non-existent physical file: {file_abs_path}")
    
    except Exception as e:
        logger.error(f"Error sending file from button: {e}")
        await query.answer("حدث خطأ أثناء إرسال الملف.", show_alert=True)
        
        
async def handle_button_press(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    (النسخة الكاملة والمحدثة)
    تتعامل مع جميع ضغطات الأزرار وتستدعي دالة التنزيل بالمسار النسبي.
    """
    query = update.callback_query
    await query.answer()

    data = query.data
    user_id = query.from_user.id
    user_username = query.from_user.username

    logger.info(f"User {user_username} (ID: {user_id}) pressed button with data: '{data}'")
    
    root_abs_path = os.path.abspath(FILES_DIR)

    # --- 1. منطق الرفع الذكي ---
    if data.startswith("nav_upload_"):
        relative_path = data[len("nav_upload_"):]
        path_to_navigate = os.path.abspath(os.path.join(root_abs_path, relative_path))
        await show_upload_destination_menu(update, context, path_to_navigate)
        return
        
    elif data.startswith("upload_to_"):
        relative_path = data[len("upload_to_"):]
        destination_path = os.path.abspath(os.path.join(root_abs_path, relative_path))
        
        pending_file = context.user_data.pop('pending_upload', None)
        if not pending_file:
            await query.edit_message_text("عذرًا، يبدو أن هذه الجلسة قد انتهت. الرجاء إرسال الملف مرة أخرى.")
            return

        await query.edit_message_text(f"جاري حفظ الملف `{pending_file['file_name']}`...")

        try:
            bot_file = await context.bot.get_file(pending_file['file_id'])
            
            base_name, ext = os.path.splitext(pending_file['file_name'])
            counter = 1
            final_path = os.path.join(destination_path, pending_file['file_name'])
            
            while os.path.exists(final_path):
                new_file_name = f"{base_name}_{counter}{ext}"
                final_path = os.path.join(destination_path, new_file_name)
                counter += 1

            await bot_file.download_to_drive(final_path)
            
            with sqlite3.connect(DB_FILE) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO files (file_name, file_path, size_bytes, uploaded_by) VALUES (?, ?, ?, ?)",
                    (os.path.basename(final_path), final_path, pending_file['file_size'], user_id)
                )
                conn.commit()

            await query.answer(f"✅ تم حفظ الملف بنجاح!", show_alert=False)
            logger.info(f"User {user_username} completed upload of '{os.path.basename(final_path)}' to '{destination_path}'.")
            await list_files_with_buttons(query.message, context, destination_path)

        except Exception as e:
            logger.error(f"Error during final file save operation: {e}")
            await query.edit_message_text("حدث خطأ فادح أثناء حفظ الملف.")
        return

    elif data == "cancel_upload":
        context.user_data.pop('pending_upload', None)
        await query.edit_message_text("تم إلغاء عملية الرفع.")
        return

    # --- 2. منطق الحذف التفاعلي ---
    elif data == "admin_delete_start":
        await show_deletion_menu(update, context, root_abs_path)
        return
        
    elif data.startswith("nav_delete_"):
        relative_path = data[len("nav_delete_"):]
        path_to_navigate = os.path.abspath(os.path.join(root_abs_path, relative_path))
        await show_deletion_menu(update, context, path_to_navigate)
        return

    elif data.startswith("confirm_delete_"):
        relative_path = data[len("confirm_delete_"):]
        item_name = os.path.basename(relative_path)
        parent_rel_path = os.path.dirname(relative_path)
        
        keyboard = [[
            InlineKeyboardButton("✅ نعم، احذف الآن", callback_data=f"execute_delete_{relative_path}"),
            InlineKeyboardButton("❌ لا، إلغاء", callback_data=f"nav_delete_{parent_rel_path}")
        ]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(f"⚠️ هل أنت متأكد من حذف '{item_name}'؟\n\n**لا يمكن التراجع عن هذا الإجراء!**", reply_markup=reply_markup, parse_mode='Markdown')
        return

    elif data.startswith("execute_delete_"):
        relative_path = data[len("execute_delete_"):]
        item_abs_path = os.path.abspath(os.path.join(root_abs_path, relative_path))
        
        success, message = await delete_item_logic(item_abs_path)
        await query.answer(message, show_alert=True)
        
        parent_abs_path = os.path.dirname(item_abs_path)
        await show_deletion_menu(update, context, parent_abs_path)
        return
        
    elif data == "noop":
        return

    # --- 3. منطق إنشاء المجلدات التفاعلي ---
    elif data.startswith("nav_create_"):
        relative_path = data[len("nav_create_"):]
        path_to_navigate = os.path.abspath(os.path.join(root_abs_path, relative_path))
        await show_folder_creation_menu(update, context, path_to_navigate)
        return

    elif data.startswith("create_here_"):
        relative_path = data[len("create_here_"):]
        creation_path = os.path.abspath(os.path.join(root_abs_path, relative_path))
        context.user_data['user_action'] = 'awaiting_new_folder_name'
        context.user_data['creation_path'] = creation_path
        
        dir_name = os.path.basename(creation_path) if creation_path != root_abs_path else "الجذر"
        await query.edit_message_text(f"تم اختيار الإنشاء في: `{dir_name}`\n\nالآن، أرسل اسم المجلد الجديد.", parse_mode='Markdown')
        return

    # --- 4. منطق تصفح الملفات وتنزيلها ---
    elif data.startswith("ls_"):
        target_path_segment = data[len("ls_"):]
        current_path_key = f"{user_id}_current_path"
        current_path = context.user_data.get(current_path_key, root_abs_path)

        if target_path_segment == "root": new_path = root_abs_path
        elif target_path_segment == "..": new_path = os.path.dirname(current_path)
        elif target_path_segment == ".": new_path = current_path
        else: new_path = os.path.join(current_path, target_path_segment)
        
        abs_new_path = os.path.abspath(new_path)
        if not abs_new_path.startswith(root_abs_path):
            await query.edit_message_text("عذرًا، لا يمكنك الوصول إلى هذا المسار.")
            return

        context.user_data[current_path_key] = abs_new_path
        await list_files_with_buttons(query.message, context, abs_new_path)
        return

    elif data.startswith("download_"):
        # الاستدعاء الصحيح للدالة الجديدة
        relative_path_to_download = data[len("download_"):]
        await download_file_from_button(query, context, relative_path_to_download)
        return

    # --- 5. أزرار القوائم العامة والإدارية ---
    elif data == "main_menu":
        await send_main_keyboard(update, context)
    elif data == "admin_menu":
        await send_admin_menu(update, context)
    elif data == "admin_roles_menu":
        await send_admin_roles_menu(update, context)
    elif data == "my_role":
        await my_role(update, context)
    elif data == "contact_admin_btn":
        await query.edit_message_text(
            "للتواصل مع الإدارة، استخدم الأمر: `/contact_admin <رسالتك>`",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu")]])
        )
    elif data == "admin_newfolder":
        if is_admin_or_higher(user_id):
            await show_folder_creation_menu(update, context, root_abs_path)
        else:
            await query.answer("عذرًا، أنت لا تملك الصلاحية للوصول لهذه الميزة.", show_alert=True)
        return
    elif data == "admin_stats_button":
        await show_stats_from_button(update, context)
    elif data == "admin_list_admins_button":
        await list_admins_from_button(update, context)
        
    # --- 6. الأزرار التي تعرض مساعدة نصية للأوامر ---
    elif data == "admin_delete":
        await query.edit_message_text(
            "تم استبدال هذا الأمر بواجهة الحذف التفاعلية. استخدم زر 'حذف ملف/مجلد (تفاعلي)'.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ عودة", callback_data="admin_menu")]])
        )
    elif data == "admin_upload_info":
        await query.edit_message_text(
            "لرفع ملف، قم بإرساله مباشرة إلى البوت في أي وقت.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ عودة", callback_data="admin_menu")]])
        )
    elif data == "admin_set_role":
        await query.edit_message_text(
            "لتعيين دور: `/addadmin @username <role>`",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ عودة", callback_data="admin_roles_menu")]])
        )
    elif data == "admin_remove_role":
        await query.edit_message_text(
            "لإزالة دور: `/removeadmin @username`",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ عودة", callback_data="admin_roles_menu")]])
        )
    elif data == "admin_broadcast_button":
        await query.edit_message_text(
            "لبث رسالة: `/broadcast <الرسالة>`",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ عودة", callback_data="admin_menu")]])
        )
        
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يسجل الأخطاء ويبلغ المطور."""
    logger.error("Exception while handling an update:", exc_info=context.error)
    if update and update.effective_message:
        await update.effective_message.reply_text("عذرًا، حدث خطأ غير متوقع. تم إبلاغ المطور.")


# --- بداية الكود الجديد ---

# إنشاء تطبيق فلاسك بسيط جداً
# وظيفته الوحيدة هي الرد على طلبات خدمة الإيقاظ
app = Flask(__name__)

@app.route('/')
def hello():
    """هذه هي الصفحة التي ستقوم خدمة UptimeRobot بزيارتها."""
    return "I am alive and the bot is running!"

def run_bot():
    """هذه الدالة تحتوي على الكود الأصلي لتشغيل البوت."""
    # 1. إعداد قاعدة البيانات والجداول عند بدء التشغيل
    setup_database()

    # 2. إنشاء كائن التطبيق وربطه بالتوكن
    # تأكد من أن التوكن يقرأ من متغيرات البيئة
    TOKEN = os.environ.get("TELEGRAM_TOKEN")
    if not TOKEN:
        logger.critical("TELEGRAM_TOKEN environment variable not set!")
        return
        
    application = Application.builder().token(TOKEN).build()
    
    # 3. تسجيل كل معالجات الأوامر (Command Handlers)
    # ... (كل أوامر application.add_handler الخاصة بك تبقى هنا كما هي)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("myrole", my_role))
    application.add_handler(CommandHandler("contact_admin", contact_admin))
    application.add_handler(CommandHandler("newfolder", new_folder))
    application.add_handler(CommandHandler("delete", delete_item))
    application.add_handler(CommandHandler("stats", show_stats))
    application.add_handler(CommandHandler("addadmin", add_admin))
    application.add_handler(CommandHandler("removeadmin", remove_admin))
    application.add_handler(CommandHandler("listadmins", list_admins))
    application.add_handler(CommandHandler("broadcast", broadcast_message))

    # 4. تسجيل معالج الوسائط
    application.add_handler(MessageHandler(
        (filters.Document.ALL | filters.PHOTO | filters.VIDEO) & ~filters.COMMAND, 
        handle_media_upload
    ))
    
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_handler))

    # 5. تسجيل معالج الأزرار
    application.add_handler(CallbackQueryHandler(handle_button_press))

    # 6. تسجيل معالج الأخطاء
    application.add_error_handler(error_handler)

    # 7. بدء تشغيل البوت
    print("Bot is starting via polling...")
    application.run_polling()

# الدالة الرئيسية الجديدة التي سيتم تشغيلها
def main():
    # تشغيل البوت في خيط منفصل
    bot_thread = threading.Thread(target=run_bot)
    bot_thread.start()
    
    # تشغيل خادم الويب في الخيط الرئيسي
    # Railway سيوفر متغير PORT تلقائياً
    port = int(os.environ.get('PORT', 8080))
    print(f"Flask web server starting on port {port}...")
    app.run(host='0.0.0.0', port=port)

if __name__ == '__main__':
    main()

# --- نهاية الكود الجديد ---
