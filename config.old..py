import os
import ctypes
import ctypes.wintypes

def get_documents_folder_path ():
    # CSIDL_PERSONAL = 5 -> Es el identificador de "Mis Documentos" en sistemas Windows.
    csidl_personal = 5
    buf = ctypes.create_unicode_buffer(ctypes.wintypes.MAX_PATH)
    ctypes.windll.shell32.SHGetFolderPathW(None, csidl_personal, None, 0, buf)
    return buf.value

APP_NAME = 'GachaSloth'
APP_FOLDER = os.path.join(get_documents_folder_path(), APP_NAME)
APP_CONFIG_FILE_PATH = os.path.join(APP_FOLDER, f'{APP_NAME}Config.json')

DB_NAME = f'{APP_NAME}DB.db'
DB_PATH = os.path.join(APP_FOLDER, DB_NAME)
DB_INIT = """CREATE TABLE IF NOT EXISTS accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL, password TEXT NOT NULL, login_string TEXT, cookies TEXT, device_id TEXT, device_fp TEXT);"""
DB_INSERT = """INSERT INTO accounts (email, password) VALUES (?, ?)""" # (str: email, str: password)
DB_DELETE = """DELETE FROM accounts WHERE id = ?""" # (int: id,)
DB_UPDATE = """UPDATE accounts SET email = ? AND password = ? WHERE id = ?""" # (str: email, str: password, int: id)
DB_FETCH = """SELECT * FROM accounts"""

if __name__ == "__main__":
    print(APP_FOLDER)
    print(DB_PATH)
    