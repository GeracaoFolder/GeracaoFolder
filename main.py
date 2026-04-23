import streamlit as st
import yt_dlp
import os

st.title("🎥 Downloader de Vídeos do YouTube")

url = st.text_input("📥 Cole aqui o link do vídeo do YouTube:")

output_dir = "downloads"
os.makedirs(output_dir, exist_ok=True)

if url:
    try:
        st.subheader("🎯 Informações e Download")
        ydl_opts = {
            'format': 'bestvideo+bestaudio/best',
            'outtmpl': os.path.join(output_dir, '%(title)s.%(ext)s'),
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            st.write(f"**Título:** {info.get('title')}")
            st.write(f"**Duração:** {round(info.get('duration', 0) / 60, 2)} minutos")
            st.write(f"**Visualizações:** {info.get('view_count')}")

            if st.button("⬇️ Baixar e Exibir o Vídeo"):
                with st.spinner("Baixando o vídeo..."):
                    result = ydl.download([url])
                    filename = ydl.prepare_filename(info)
                    st.success("✅ Download concluído!")

                    st.subheader("📺 Visualização do Vídeo")
                    st.video(filename)

                    with open(filename, "rb") as file:
                        st.download_button(
                            label="📥 Baixar o vídeo",
                            data=file,
                            file_name=os.path.basename(filename),
                            mime="video/mp4"
                        )
    except Exception as e:
        st.error(f"Ocorreu um erro: {e}")
