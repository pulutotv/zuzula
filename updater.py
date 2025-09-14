import requests
import logging
import os
import hashlib

# Configura il logger per questo modulo
logger = logging.getLogger(__name__)

# --- CONFIGURAZIONE DELL'UPDATER ---
# Modifica questi valori se il tuo repository cambia
GITHUB_REPO = "nzo66/EasyProxy"
BRANCH = "main"

# Elenco dei file da tenere aggiornati.
# Usa os.path.join per la compatibilità tra sistemi operativi (Windows/Linux).
FILES_TO_UPDATE = [
    "app.py",
    "vavoo_extractor.py",
    "dlhd_extractor.py",
    "vixsrc_extractor.py",
    "playlist_builder.py",
    "requirements.txt",
    "README.md",
    "docker-compose.yml",
    "Dockerfile",
    "updater.py",
    os.path.join("templates", "index.html"),
    os.path.join("templates", "info.html"),
    os.path.join("templates", "builder.html"),
]

# URL base per scaricare i file raw da GitHub
BASE_RAW_URL = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{BRANCH}/"

def get_file_hash(file_path):
    """Calcola l'hash SHA256 di un file."""
    if not os.path.exists(file_path):
        return None
    hasher = hashlib.sha256()
    with open(file_path, 'rb') as f:
        buf = f.read(65536)
        while len(buf) > 0:
            hasher.update(buf)
            buf = f.read(65536)
    return hasher.hexdigest()

def check_for_updates():
    """
    Controlla e scarica gli aggiornamenti per i file specificati da GitHub.
    """
    logger.info("🚀 Avvio controllo aggiornamenti da GitHub...")
    updated_files_count = 0
    
    script_dir = os.path.dirname(os.path.abspath(__file__))

    for relative_path in FILES_TO_UPDATE:
        remote_url = BASE_RAW_URL + relative_path.replace(os.sep, "/")
        local_path = os.path.join(script_dir, relative_path)
        
        logger.info(f"🔄 Controllo file: {relative_path}")
        
        try:
            response = requests.get(remote_url, timeout=15)
            response.raise_for_status()
            remote_content = response.content
            remote_hash = hashlib.sha256(remote_content).hexdigest()
            
            local_hash = get_file_hash(local_path)
            
            if local_hash != remote_hash:
                logger.info(f"✨ Trovato aggiornamento per {relative_path}. Scaricamento in corso...")
                os.makedirs(os.path.dirname(local_path), exist_ok=True)
                with open(local_path, 'wb') as f:
                    f.write(remote_content)
                logger.info(f"✅ File {relative_path} aggiornato con successo.")
                updated_files_count += 1

        except requests.exceptions.RequestException as e:
            logger.warning(f"⚠️ Impossibile controllare/scaricare {relative_path}: {e}. Verrà usata la versione locale.")
        except Exception as e:
            logger.error(f"❌ Errore imprevisto durante l'aggiornamento di {relative_path}: {e}")

    if updated_files_count > 0:
        logger.info(f"🎉 Aggiornamento completato. {updated_files_count} file sono stati aggiornati. Riavvia l'applicazione per applicare le modifiche.")
    else:
        logger.info("✅ Tutti i file sono già alla versione più recente.")