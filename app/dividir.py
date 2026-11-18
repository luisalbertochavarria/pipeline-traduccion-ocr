# dividir.py - Divide un PDF en bloques de N paginas (uso: python dividir.py pdf out_dir n)
import os
import sys

import pymupdf


def dividir(pdf_path, carpeta_salida, tamano_lote=10):
    os.makedirs(carpeta_salida, exist_ok=True)
    doc = pymupdf.open(pdf_path)
    total = len(doc)
    for inicio in range(0, total, tamano_lote):
        fin = min(inicio + tamano_lote, total)
        sub = pymupdf.open()
        sub.insert_pdf(doc, from_page=inicio, to_page=fin - 1)
        nombre = os.path.join(carpeta_salida, f'bloque_pag_{inicio + 1}_a_{fin}.pdf')
        sub.save(nombre)
        sub.close()
        print(f'Guardado: {os.path.basename(nombre)}')
    doc.close()
    print(f'Division completada: {total} paginas en bloques de {tamano_lote}')


if __name__ == '__main__':
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 10
    dividir(sys.argv[1], sys.argv[2], n)
