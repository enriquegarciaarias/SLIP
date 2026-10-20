import streamlit as st
from pathlib import Path

st.set_page_config(page_title="SLIP - Training Materials", layout="wide")

st.title("SLIP - Training Materials Viewer")
st.markdown("Vista rápida del `training_materials.md` generado por el pipeline.")

md_path = Path("results/output/SensoresACL/training_materials.md")

if md_path.exists():
    text = md_path.read_text(encoding="utf-8")
    st.success(f"Archivo cargado: {md_path} ({len(text):,} caracteres)")
    st.markdown(text[:5000])
    with st.expander("Ver documento completo"):
        st.text_area("Contenido completo", text, height=600)
else:
    st.error(f"No se encontró {md_path}")

st.divider()
st.subheader("Estado del pipeline")
for f in ["ranked_papers.json", "selected_papers.json", "concept_findings.json", "technical_annex.json", "training_materials.md"]:
    p = Path("results/output/SensoresACL") / f
    st.write(f"- {f}: {'✅ existe' if p.exists() else '❌ ausente'}")
