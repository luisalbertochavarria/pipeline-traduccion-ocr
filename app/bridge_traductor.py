# bridge_traductor.py - Proceso persistente de traduccion (corre con python de mineru-env,
# que tiene torch+transformers). Lee peticiones JSON por stdin, responde por stdout.
# Uso: python bridge_traductor.py <modelo_dir>
import json
import sys


def main():
    modelo_dir = sys.argv[1] if len(sys.argv) > 1 else 'modelos/opus-en-es-orig'
    from transformers import MarianMTModel, MarianTokenizer

    tok = MarianTokenizer.from_pretrained(modelo_dir)
    model = MarianMTModel.from_pretrained(modelo_dir)
    model.eval()

    for linea in sys.stdin:
        linea = linea.strip()
        if not linea:
            continue
        try:
            req = json.loads(linea)
            textos = req.get('textos') or []
            if not textos:
                print(json.dumps({'id': req.get('id'), 'traducciones': []}), flush=True)
                continue
            outs = model.generate(
                **tok(textos, return_tensors='pt', padding=True, truncation=True, max_length=500),
                max_new_tokens=300,
                repetition_penalty=1.15,  # corta bucles de repeticion de Marian
            )
            trad = tok.batch_decode(outs, skip_special_tokens=True)
            print(json.dumps({'id': req.get('id'), 'traducciones': trad}), flush=True)
        except Exception as e:
            print(json.dumps({'id': req.get('id'), 'error': str(e)}), flush=True)


if __name__ == '__main__':
    main()
