# Gym Tracker

Projeto em Streamlit para acompanhar treinos registrados no WhatsApp.

## Rodar localmente

```bash
pip install -r requirements.txt
python -m streamlit run app.py
```

## Arquivos principais

- `app.py`: dashboard Streamlit.
- `academia_treinos_whatsapp.csv`: base estruturada usada pelo app.
- `gerar_csv_whatsapp.py`: parser que transforma o export do WhatsApp em CSV.
- `whatsapp_acad_export/Conversa do WhatsApp com Acad.txt`: export bruto usado pelo parser.
- `.streamlit/config.toml`: tema visual do app.

## Regenerar CSV

```bash
python gerar_csv_whatsapp.py
```

O CSV usa uma linha por série, com colunas para data, grupo muscular, exercício, tipo, peso, série e repetições.
