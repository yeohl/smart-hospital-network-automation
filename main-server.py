from flask import Flask, request, jsonify, make_response
from flask_cors import CORS
import pymysql
import mysql.connector
import socket
import time
import logging

app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO)

# ✅ RDS 설정
DB_CONFIG = {
    "host": "mysql-multi-db.cxq0qomeyx1k.us-east-1.rds.amazonaws.com",
    "user": "admin",
    "password": "zaqxsw11!",
    "database": "hospital",
    "port": 3306
}

# ✅ DB 저장 함수
def insert_patient(data):
    conn = pymysql.connect(**DB_CONFIG, charset='utf8mb4')
    try:
        with conn.cursor() as cursor:
            check_sql = """
                SELECT COUNT(*) FROM patient
                WHERE device_id = %s AND timestamp = %s
            """
            cursor.execute(check_sql, (data["device_id"], data["timestamp"]))
            count = cursor.fetchone()[0]

            if count == 0:
                insert_sql = """
                    INSERT INTO patient (
                        device_id, patient_id, patient_name,
                        ward_id, heart_rate, respiratory_rate, spo2,
                        temperature, blood_pressure, consciousness,
                        timestamp
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """
                cursor.execute(insert_sql, (
                    data.get("device_id"),
                    data.get("patient_id"),
                    data.get("patient_name"),
                    data.get("ward_id"),
                    data.get("heart_rate"),
                    data.get("respiratory_rate"),
                    data.get("spo2"),
                    data.get("temperature"),
                    data.get("blood_pressure"),
                    data.get("consciousness"),
                    data.get("timestamp")
                ))
                conn.commit()
            else:
                logging.warning(f"⛔ 중복 데이터: {data['device_id']} @ {data['timestamp']} → 저장 생략됨")
    finally:
        conn.close()

# ✅ 1. 환자 데이터 수신 (POST)
@app.route('/data', methods=['POST'])
def receive_data():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No JSON payload received"}), 400
    try:
        logging.info(f"[{data['timestamp']}] {data['patient_name']}({data['patient_id']}) → "
                     f"HR: {data['heart_rate']}, RR: {data['respiratory_rate']}, "
                     f"SpO2: {data['spo2']}%, Temp: {data['temperature']}°C, "
                     f"BP: {data['blood_pressure']}, Consciousness: {data['consciousness']}, "
                     f"Ward: {data['ward_id']}")
        insert_patient(data)
        return jsonify({"status": "saved to DB"}), 200
    except Exception as e:
        logging.error(f"DB insert failed: {e}")
        return jsonify({"error": "DB insert failed"}), 500

# ✅ 2. 대시보드 환자 조회 (GET)
@app.route("/dashboard/patients", methods=["GET"])
def get_all_patients():
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT patient_id, patient_name AS name, heart_rate, respiratory_rate,
                   spo2, temperature, blood_pressure, consciousness, timestamp, ward_id
            FROM patient
            ORDER BY timestamp DESC
            LIMIT 100
        """)
        result = cursor.fetchall()
        return jsonify(result), 200
    except Exception as e:
        logging.error(f"[❌ get_all_patients ERROR]: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()

# ✅ 3. SLA 체크용 요약 (GET)
@app.route('/dashboard/patient-summary', methods=['GET'])
def get_patient_summary():
    server_name = socket.gethostname()
    start = time.time()
    conn = None
    try:
        conn = pymysql.connect(**DB_CONFIG)
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM patient")
            result = cursor.fetchone()
    except Exception as e:
        response = make_response(jsonify({"error": str(e)}), 500)
        response.headers['X-Server'] = server_name  # ✅ 예외에도 헤더 추가
        return response
    finally:
        if conn:
            conn.close()

    total_time = round((time.time() - start) * 1000, 2)
    response = make_response(jsonify({
        "status": "ok",
        "server": server_name,
        "patient_count": result[0],
        "timestamp": time.strftime('%Y-%m-%d %H:%M:%S'),
        "db_elapsed_ms": total_time
    }))
    response.headers['X-Server'] = server_name
    return response, 200

# ✅ 4. 헬스체크
@app.route('/health', methods=['GET'])
def health_check():
    return "healthy", 200

# ✅ 실행
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
