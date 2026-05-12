import sqlite3
import os

import config

def exists_or_create_app_folder ():
    if not os.path.exists (config.APP_FOLDER):
        os.mkdir(config.APP_FOLDER)

class DatabaseManager:
    def __init__(self, db: str = config.DB_PATH):
        self.db = db
        self.connection = None
        self.cursor = None
        exists_or_create_app_folder()
        if not os.path.exists (config.DB_PATH): self.db_generation ()

    def connect (self):
        if not self.connection:
            self.connection = sqlite3.connect (self.db)
            self.cursor = self.connection.cursor()
        return self

    def close (self):
        self.connection.commit()
        if self.cursor:
            self.cursor.close()
        if self.connection:
            self.connection.close()
        self.cursor = None
        self.connection = None
    
    def execute(self, sql : str, params: tuple = None):
        self.cursor.execute(sql, params)
        self.connection.commit()
    
    def get_data (self, sql : str = config.DB_FETCH, params: tuple = None):
        self.cursor.execute(sql)
        return self.cursor.fetchall()

    def db_generation (self):
        self.connect()
        self.cursor.execute(config.DB_INIT)
        self.connection.commit()
        self.close()

    def __enter__(self):
        self.connect()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            # Rollback si hay excepción
            if self.connection:
                self.connection.rollback()
        self.close()

if __name__ == "__main__":
    db = DatabaseManager()
    db.connect()

    db.close()