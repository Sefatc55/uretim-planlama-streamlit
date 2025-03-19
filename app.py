import streamlit as st
import pandas as pd
import sqlite3
import gurobipy as gp
from gurobipy import GRB
import plotly.express as px
from datetime import datetime, timedelta
from io import BytesIO

st.set_page_config(layout="wide")
st.title(" HASSAS PLASTİK Üretim Planlama Sistemi")

conn = sqlite3.connect('uretim.db')
df_veri_total = pd.read_sql("SELECT * FROM VeriTotal", conn)
setup_df = pd.read_sql("SELECT * FROM SetupMatrisi", conn)
conn.close()

setup_times = {(row["Onceki_StokKodu"], row["Sonraki_StokKodu"]): row["Setup_Suresi"] for _, row in setup_df.iterrows()}

selected_products = st.multiselect("📌 Planlanacak Ürünleri Seçin:", df_veri_total["ÜRÜN ADI"].unique())
quantities = {}
for product in selected_products:
    quantities[product] = st.number_input(f"➕ '{product}' üretim miktarı (adet):", min_value=1, value=1)

if st.button("🚀 Üretimi Planla"):
    if not selected_products:
        st.warning("⚠️ Lütfen en az bir ürün seçin!")
    else:
        df = df_veri_total[df_veri_total["ÜRÜN ADI"].isin(selected_products)].reset_index(drop=True)
        df["Miktar"] = df["ÜRÜN ADI"].map(quantities)
        df["LEVHA_PROSES"] = (df["LEVHA PROSES SÜRESİ"] * df["Miktar"]) / 60  # dakika
        df["VAKUM_PROSES"] = (df["VAKUM PROSES SÜRESİ"] * df["Miktar"]) / 60
        df["KESIM_PROSES"] = (df["ÇAPAK ALMA SÜRESİ"] * df["Miktar"]) / 60

        job_codes = df["STOK KODU"].tolist()
        job_times_1 = df["LEVHA_PROSES"].tolist()
        job_times_2 = df["VAKUM_PROSES"].tolist()
        job_times_3 = df["KESIM_PROSES"].tolist()
        machines_2 = df["VAKUM MAKİNESİ"].tolist()
        machines_3 = df["KESİM TÜRÜ"].tolist()
        num_jobs = len(df)

        model = gp.Model("PlanlamaModeli")
        start_times_1 = model.addVars(num_jobs, vtype=GRB.CONTINUOUS, name="Start_Extruder")
        start_times_2 = model.addVars(num_jobs, vtype=GRB.CONTINUOUS, name="Start_Vakum")
        start_times_3 = model.addVars(num_jobs, vtype=GRB.CONTINUOUS, name="Start_Kesim")
        makespan = model.addVar(vtype=GRB.CONTINUOUS, name="Makespan")

        model.setObjective(makespan, GRB.MINIMIZE)

        # Ekstruder başlangıç ısınma süresi
        extruder_warmup = 180
        model.addConstr(start_times_1[0] >= extruder_warmup)

        # Ekstruder aşaması ve setup
        for i in range(num_jobs):
            if i > 0:
                setup_time = setup_times.get((job_codes[i - 1], job_codes[i]), 0)
                model.addConstr(start_times_1[i] >= start_times_1[i - 1] + job_times_1[i - 1] + setup_time)
            model.addConstr(start_times_1[i] + job_times_1[i] <= start_times_2[i])

        # Vakum makineleri paralel çalışıyor
        vakum_warmup = 30
        vakum_makine_zaman = {}
        unique_vakum = list(set(machines_2))
        for m in unique_vakum:
            vakum_makine_zaman[m] = vakum_warmup

        for i in range(num_jobs):
            makine = machines_2[i]
            onceki = vakum_makine_zaman[makine]
            model.addConstr(start_times_2[i] >= start_times_1[i] + job_times_1[i])
            model.addConstr(start_times_2[i] >= onceki)
            vakum_makine_zaman[makine] = start_times_2[i] + job_times_2[i] + 30  # Setup arası
            model.addConstr(start_times_2[i] + job_times_2[i] <= start_times_3[i])

        # Kesim aşaması
        for i in range(num_jobs):
            model.addConstr(start_times_3[i] >= start_times_2[i] + job_times_2[i])
            model.addConstr(start_times_3[i] + job_times_3[i] <= makespan)

        model.optimize()

        if model.status == GRB.OPTIMAL:
            baslangic = datetime.now().replace(hour=7, minute=0, second=0, microsecond=0)
            planlama = []
            for i in range(num_jobs):
                start_1 = baslangic + timedelta(minutes=start_times_1[i].X)
                finish_1 = start_1 + timedelta(minutes=job_times_1[i])
                start_2 = baslangic + timedelta(minutes=start_times_2[i].X)
                finish_2 = start_2 + timedelta(minutes=job_times_2[i])
                start_3 = baslangic + timedelta(minutes=start_times_3[i].X)
                finish_3 = start_3 + timedelta(minutes=job_times_3[i])
                planlama.extend([
                    {"İş": f"{job_codes[i]} - Ekstrüder", "Başlangıç": start_1, "Bitiş": finish_1, "Makine": "Ekstrüder"},
                    {"İş": f"{job_codes[i]} - Vakum ({machines_2[i]})", "Başlangıç": start_2, "Bitiş": finish_2, "Makine": machines_2[i]},
                    {"İş": f"{job_codes[i]} - Kesim ({machines_3[i]})", "Başlangıç": start_3, "Bitiş": finish_3, "Makine": machines_3[i]}
                ])

            df_plan = pd.DataFrame(planlama)
            df_plan.sort_values(by="Başlangıç", inplace=True)

            # Hafta sonu kontrolü
            new_rows = []
            for _, row in df_plan.iterrows():
                start, end = row["Başlangıç"], row["Bitiş"]
                while True:
                    if start.weekday() == 5 and start.hour >= 23 or start.weekday() == 6:
                        start = start + timedelta(days=(7 - start.weekday()))  # Pazartesiye at
                        start = start.replace(hour=7, minute=0, second=0)
                        end = start + (row["Bitiş"] - row["Başlangıç"])
                    else:
                        break
                new_rows.append({"İş": row["İş"], "Başlangıç": start, "Bitiş": end, "Makine": row["Makine"]})
            df_plan = pd.DataFrame(new_rows)

            st.success(f"✅ Planlama Tamamlandı! Toplam Süre: {makespan.X:.2f} dakika")

            # Gantt Chart
            fig = px.timeline(df_plan, x_start="Başlangıç", x_end="Bitiş", y="İş", color="Makine", title="📊 Üretim Gantt Şeması")
            fig.update_yaxes(autorange="reversed")
            fig.update_layout(xaxis_title="Zaman", yaxis_title="İşler", height=700)
            st.plotly_chart(fig, use_container_width=True)

            # Excel çıktısı
            excel_buffer = BytesIO()
            with pd.ExcelWriter(excel_buffer, engine='xlsxwriter') as writer:
                df_plan.to_excel(writer, index=False, sheet_name='Üretim Planı')
            excel_buffer.seek(0)
            st.download_button("📥 Excel Çıktısını İndir", data=excel_buffer, file_name="Uretim_Plani.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        else:
            st.error("❌ Optimum çözüm bulunamadı. Girdi verilerini ve kısıtları kontrol edin!")

            #streamlit run app.py