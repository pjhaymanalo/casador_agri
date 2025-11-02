from flask import Flask, render_template, request, flash, redirect, url_for, session, jsonify
from datetime import datetime, date, timedelta
import mysql.connector
from flask_mail import Mail, Message
import plotly.graph_objs as go
import plotly.offline as pyo
import pandas as pd
import importlib
try:
    sarimax_mod = importlib.import_module('statsmodels.tsa.statespace.sarimax')
    SARIMAX = getattr(sarimax_mod, 'SARIMAX')
except Exception:
    class SARIMAX:
        def __init__(self, *args, **kwargs):
            raise ImportError("statsmodels.tsa.statespace.sarimax.SARIMAX is not available; install the 'statsmodels' package to enable forecasting.")
import os, time, requests, io, csv

def get_db_connection():
    return mysql.connector.connect(
        host="localhost",
        user="root",
        password="",
        database="casadoragri_db"
    )

app = Flask(__name__)
app.secret_key = 'your_secret_key'
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = 'maytrixiem@gmail.com'
app.config['MAIL_PASSWORD'] = 'sisp whwg gtoj txjq'
mail = Mail(app)

# Replace the old API key with your new one
API_KEY = 'AIzaSyCKoLuO0tW46lMavIXdnV3sBnvpvpAeyJg'  # Replace with the key you just created
DB_CONFIG = {
    'host': '127.0.0.1',
    'user': 'root',
    'password': '',
    'database': 'casadoragri_db'
}

def geocode(address):
    url = 'https://maps.googleapis.com/maps/api/geocode/json'
    r = requests.get(url, params={'address': address, 'key': API_KEY})
    j = r.json()
    if j.get('status') == 'OK' and j.get('results'):
        loc = j['results'][0]['geometry']['location']
        return float(loc['lat']), float(loc['lng'])
    return None, None

def get_full_name(user_id):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT full_name FROM users WHERE user_id = %s", (user_id,))
    result = cursor.fetchone()
    conn.close()
    return result['full_name'] if result else None

@app.route('/')
def home():
    products = ['Corn', 'Hog', 'Poultry', 'Cattle', 'Cat', 'Dog']
    current_year = datetime.now().year
    return render_template('home.html', products=products, current_year=current_year)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        full_name = request.form.get('name')
        username = request.form.get('username')
        email = request.form.get('email')
        password = request.form.get('password')
        role = request.form.get('role')
        agree = request.form.get('agree')

        if not all([full_name, username, email, password, role, agree]):
            flash('All fields including role and agreement must be filled.', 'error')
            return redirect(url_for('register'))

        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            insert_query = """
                INSERT INTO users (full_name, email, password, role, status)
                VALUES (%s, %s, %s, %s, 'pending')
            """
            cursor.execute(insert_query, (full_name, email, password, role))
            conn.commit()
            conn.close()
            flash('Registration successful! Your account is pending admin approval.', 'success')
        except mysql.connector.Error as err:
            flash(f'Database error: {err}', 'error')

        return redirect(url_for('register'))

    return render_template('register.html', current_year=datetime.now().year)

@app.route('/login', methods=['GET', 'POST'])
def login():
    current_year = datetime.now().year

    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')

        if not email or not password:
            flash('Please enter both email and password.', 'error')
            return redirect(url_for('login'))

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE email = %s", (email,))
        user = cursor.fetchone()

        if user:
            stored_password = user[3]
            role = user[4]
            status = user[5]

            if status != 'approved':
                conn.close()
                flash('Your account is not yet approved.', 'error')
                return redirect(url_for('login'))

            if password == stored_password:
                session['user_id'] = user[0]
                session['role'] = role
                session['email'] = user[2]

                # Log login
                full_name = user[1]
                cursor.execute(
                    "INSERT INTO activity_log (full_name, action) VALUES (%s, %s)",
                    (full_name, f"{role} logged in")
                )
                conn.commit()
                conn.close()

                flash('Login successful!', 'success')

                if role == 'admin':
                    return redirect(url_for('admin_dashboard'))
                elif role == 'warehouse_staff':
                    return redirect(url_for('warehouse_dashboard'))
                elif role == 'secretary':
                    return redirect(url_for('secretary_dashboard'))
                elif role == 'delivery_driver':
                    return redirect(url_for('delivery_dashboard'))
                else:
                    flash('Unknown role. Please contact admin.', 'error')
                    return redirect(url_for('login'))
            else:
                conn.close()
                flash('Incorrect password.', 'error')
        else:
            flash('Email not found.', 'error')

    return render_template('login.html', current_year=current_year)

@app.route('/admin/dashboard')
def admin_dashboard():
    if 'role' not in session or session['role'] != 'admin':
        flash("Access denied.", "error")
        return redirect(url_for('login'))

    period = request.args.get('period')
    date_filter = ""
    params = []

    # Add filter logic for dropdown
    if period == "today":
        date_filter = "WHERE sale_date = CURDATE()"
    elif period == "yesterday":
        date_filter = "WHERE sale_date = CURDATE() - INTERVAL 1 DAY"
    elif period == "this_month":
        date_filter = "WHERE YEAR(sale_date) = YEAR(CURDATE()) AND MONTH(sale_date) = MONTH(CURDATE())"
    elif period == "last_week":
        date_filter = "WHERE YEARWEEK(sale_date, 1) = YEARWEEK(CURDATE(), 1) - 1"
    elif period == "last_month":
        date_filter = "WHERE YEAR(sale_date) = YEAR(CURDATE() - INTERVAL 1 MONTH) AND MONTH(sale_date) = MONTH(CURDATE() - INTERVAL 1 MONTH)"
    elif period == "5_years_ago":
        date_filter = "WHERE YEAR(sale_date) = YEAR(CURDATE()) - 5"
    else:
        date_filter = ""

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    # Monthly sales for SARIMA (with filter)
    cursor.execute(f"""
        SELECT DATE_FORMAT(sale_date, '%Y-%m') AS sale_month, SUM(total_amount) AS total_sales
        FROM sales
        {date_filter}
        GROUP BY sale_month
        ORDER BY sale_month
    """)
    monthly_sales = cursor.fetchall()
    months = [row['sale_month'] for row in monthly_sales]
    sales = [float(row['total_sales']) if row['total_sales'] is not None else 0.0 for row in monthly_sales]

    # If walang data, set chart_html and best_selling_chart_html to empty string
    if not months or not sales or sum(sales) == 0:
        chart_html = ""
        best_selling_chart_html = ""
        best_selling_forecast_html = ""
    else:
        # Log for debugging so you can confirm months and sales values in the server console
        try:
            app.logger.info(f"Monthly sales months={months}")
            app.logger.info(f"Monthly sales values={sales}")
        except Exception:
            pass
        # convert months (strings like 'YYYY-MM') to ISO date strings (first day of month)
        # Plotly expects date-like x-values for date formatting; using full date strings avoids numeric/percent rendering
        months_dt = [pd.to_datetime(m).strftime('%Y-%m-%d') for m in months] if months else []

        # SARIMA Forecast
        if months and sales:
            sales_series = pd.Series(sales, index=pd.to_datetime(months))
            try:
                model = SARIMAX(sales_series, order=(1,1,1), seasonal_order=(1,1,1,12), enforce_stationarity=False, enforce_invertibility=False)
                model_fit = model.fit(disp=False)
                forecast = model_fit.forecast(steps=1)
                forecast_value = float(forecast.iloc[0])
                forecast_month = (sales_series.index[-1] + pd.DateOffset(months=1)).strftime('%Y-%m')
            except Exception:
                forecast_value = 0
                forecast_month = (pd.to_datetime(months[-1]) + pd.DateOffset(months=1)).strftime('%Y-%m')
        else:
            forecast_value = 0
            forecast_month = None

        # Plotly chart for sales + forecast (line for actual sales)
        trace_actual = go.Scatter(
            x=months_dt,
            y=sales,
            mode='lines+markers',
            name='Actual Sales',
            line=dict(color='green', width=3, shape='spline'),
            marker=dict(size=6)
        )

        data = [trace_actual]
        # Instead of adding a forecast marker (which can force an extra tick), show forecast as an annotation
        annotations = []
        if forecast_month and forecast_value is not None:
            try:
                # Show a concise annotation in the top-right of the chart (paper coords) so axis ticks aren't affected
                ann_text = f"SARIMA forecast (next month): ₱{forecast_value:,.0f}"
                annotations.append(dict(
                    xref='paper', yref='paper', x=0.98, y=0.98,
                    xanchor='right', yanchor='top', text=ann_text,
                    showarrow=False, bgcolor='orange', font=dict(color='white', size=12)
                ))
            except Exception:
                pass

        # Force x-axis ticks to the exact months present to avoid Plotly adding an extra tick
        if months_dt:
            # tick values must match the x data format (ISO date strings)
            tickvals = months_dt.copy()
            # tick text: month abbreviation + year (e.g., 'Jan 2024')
            ticktext = [pd.to_datetime(m).strftime('%b %Y') for m in months]
            # If the first and last month share the same month+year (rare), remove the trailing tick
            try:
                first_dt = pd.to_datetime(months[0])
                last_dt = pd.to_datetime(months[-1])
                if len(tickvals) > 1 and first_dt.month == last_dt.month and first_dt.year == last_dt.year:
                    tickvals = tickvals[:-1]
                    ticktext = ticktext[:-1]
            except Exception:
                pass
            xaxis_dict = dict(
                type='date',
                title='Month',
                tickmode='array',
                tickvals=tickvals,
                ticktext=ticktext,
                tickangle=0
            )
        else:
            xaxis_dict = dict(type='date', title='Month', tickformat='%b', dtick='M1')

        layout = go.Layout(
            title='Monthly Sales with SARIMA Forecast',
            xaxis=xaxis_dict,
            yaxis=dict(title='Sales (₱)', tickprefix='₱', tickformat=',.0f'),
            hovermode='x',
            annotations=annotations
        )
        fig = go.Figure(data=data, layout=layout)
        chart_html = pyo.plot(fig, output_type='div', include_plotlyjs=False)

        # --- Best Selling Product for the latest month ---
        best_month = months[-1] if months else None
        best_selling_chart_html = ""
        if best_month:
            cursor.execute("""
            SELECT p.product_name, SUM(oi.quantity) as total_qty
            FROM order_items oi
            JOIN orders o ON oi.order_id = o.order_id
            JOIN products p ON oi.product_id = p.product_id
            WHERE DATE_FORMAT(o.date_created, '%%Y-%%m') = %s
            AND o.status IN ('completed', 'pending')
            GROUP BY p.product_name
            ORDER BY total_qty DESC
        """, (best_month,))
            product_sales = cursor.fetchall()
            if product_sales:
                prod_names = [row['product_name'] for row in product_sales]
                prod_qtys = [row['total_qty'] for row in product_sales]
                prod_trace = go.Bar(x=prod_names, y=prod_qtys, marker_color='green')
                prod_layout = go.Layout(
                    title=f'Best Selling Products for {best_month}',
                    xaxis=dict(title='Product'),
                    yaxis=dict(title='Quantity Sold')
                )
                prod_fig = go.Figure(data=[prod_trace], layout=prod_layout)
                best_selling_chart_html = pyo.plot(prod_fig, output_type='div', include_plotlyjs=False)

        # --- Predicted best selling products for next month using SARIMA (per-product forecast) ---
        # Aggregate monthly quantities per product
        cursor.execute("""
            SELECT DATE_FORMAT(o.date_created, '%Y-%m') AS sale_month, p.product_name, SUM(oi.quantity) as qty
            FROM order_items oi
            JOIN orders o ON oi.order_id = o.order_id
            JOIN products p ON oi.product_id = p.product_id
            WHERE o.status IN ('completed', 'pending')
            GROUP BY sale_month, p.product_name
            ORDER BY sale_month
        """)
        rows = cursor.fetchall()
        best_selling_forecast_html = ""
        try:
            if rows:
                df_rows = pd.DataFrame(rows)
                # convert sale_month to datetime index
                df_rows['sale_month'] = pd.to_datetime(df_rows['sale_month'])
                pivot = df_rows.pivot_table(index='sale_month', columns='product_name', values='qty', aggfunc='sum').fillna(0)

                forecasts = {}
                for product in pivot.columns:
                    series = pivot[product].astype(float)
                    # if all zero, forecast zero
                    if series.sum() == 0:
                        forecasts[product] = 0.0
                        continue
                    # if too few points, fallback to last observed
                    try:
                        if len(series.dropna()) < 3:
                            fval = float(series.iloc[-1])
                        else:
                            model_p = SARIMAX(series, order=(1,1,1), seasonal_order=(1,1,1,12), enforce_stationarity=False, enforce_invertibility=False)
                            fit_p = model_p.fit(disp=False)
                            f = fit_p.forecast(steps=1)
                            fval = float(f.iloc[0])
                            if fval < 0:
                                fval = 0.0
                    except Exception:
                        # fallback
                        fval = float(series.iloc[-1]) if len(series) > 0 else 0.0
                    forecasts[product] = fval

                # Build top-5 predicted products chart
                forecast_items = sorted(forecasts.items(), key=lambda x: x[1], reverse=True)
                top_items = forecast_items[:3]  # <-- CHANGE 5 to 3 here
                if top_items:
                    names = [it[0] for it in top_items]
                    vals = [it[1] for it in top_items]
                    pred_trace = go.Bar(x=names, y=vals, marker_color='orange')
                    pred_layout = go.Layout(
                        title='Predicted Best Selling Products (Next Month)',
                        xaxis=dict(title='Product'),
                        yaxis=dict(title='Predicted Quantity')
                    )
                    pred_fig = go.Figure(data=[pred_trace], layout=pred_layout)
                    best_selling_forecast_html = pyo.plot(pred_fig, output_type='div', include_plotlyjs=False)
        except Exception:
            best_selling_forecast_html = ""

    cursor.execute("""
        SELECT IFNULL(SUM(total_amount), 0) as total_sales
        FROM sales
        WHERE sale_date = CURDATE()
    """)
    total_sales = cursor.fetchone()['total_sales']

    cursor.execute("SELECT COUNT(*) as pending_users FROM users WHERE status='pending'")
    pending_users = cursor.fetchone()['pending_users']

    conn.close()

    return render_template(
        'admin/dashboard.html',
        total_sales=total_sales,
        pending_users=pending_users,
        current_year=date.today().year,
        chart_html=chart_html,
        best_selling_chart_html=best_selling_chart_html,
        best_selling_forecast_html=best_selling_forecast_html
    )

@app.route('/admin/users')
def view_users():
    if 'role' not in session or session['role'] != 'admin':
        flash("Access denied.", "error")
        return redirect(url_for('login'))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    # Only get pending users
    cursor.execute("SELECT user_id, full_name, email, role, status, date_registered FROM users WHERE status='pending' ORDER BY date_registered DESC")
    users = cursor.fetchall()
    conn.close()

    return render_template('admin/users.html', users=users, current_year=datetime.now().year)

@app.route('/admin/approve/<int:user_id>')
def approve_user(user_id):
    if 'role' not in session or session['role'] != 'admin':
        flash("Access denied.", "error")
        return redirect(url_for('login'))

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET status = 'approved' WHERE user_id = %s", (user_id,))
    conn.commit()
    conn.close()
    flash("User approved successfully!", "success")
    return redirect(url_for('view_users'))


@app.route('/admin/reject/<int:user_id>')
def reject_user(user_id):
    if 'role' not in session or session['role'] != 'admin':
        flash("Access denied.", "error")
        return redirect(url_for('login'))

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET status = 'rejected' WHERE user_id = %s", (user_id,))
    conn.commit()
    conn.close()
    flash("User rejected.", "success")
    return redirect(url_for('view_users'))

@app.route('/admin/update-account', methods=['POST'])
def update_account():
    if 'user_id' not in session:
        flash("Unauthorized access.", "error")
        return redirect(url_for('login'))

    new_email = request.form.get('email')
    new_password = request.form.get('password')

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET email=%s, password=%s WHERE user_id=%s",
                   (new_email, new_password, session['user_id']))
    conn.commit()
    session['email'] = new_email

    admin_full_name = get_full_name(session.get('user_id'))
    cursor.execute(
        "INSERT INTO activity_log (full_name, action) VALUES (%s, %s)",
        (admin_full_name, "Updated admin account")
    )
    conn.commit()
    conn.close()

    flash("Account updated successfully!", "success")
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/inventory', methods=['GET'])
def admin_inventory():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    # Kunin date filter
    selected_date = request.args.get('date', None)
    if not selected_date:
        cursor.execute("SELECT CURDATE() AS today")
        selected_date = cursor.fetchone()['today']

    # Query para kunin AM, PM (latest per day per product)
    query = """
        SELECT 
            p.category,
            p.product_id,
            p.product_name,
            p.price,
            p.kilo_per_unit,
            p.reorder_level,
            COALESCE(MAX(il.am), 0) AS am,
            COALESCE(MAX(il.pm), 0) AS pm
        FROM products p
        LEFT JOIN inventorylog il
            ON p.product_id = il.product_id
            AND DATE(il.date) = %s
        GROUP BY p.product_id, p.category, p.product_name, p.price, p.kilo_per_unit, p.reorder_level
        ORDER BY p.category, p.product_name
    """
    cursor.execute(query, (selected_date,))
    products = cursor.fetchall()

    # Add final_stock = pm
    for product in products:
        product['final_stock'] = product['pm']

    # Log activity: admin viewed inventory
    if 'user_id' in session:
        full_name = get_full_name(session.get('user_id'))
        log_cursor = conn.cursor()
        log_cursor.execute(
            "INSERT INTO activity_log (full_name, action, timestamp) VALUES (%s, %s, NOW())",
            (full_name, f"Viewed inventory for {selected_date}")
        )
        conn.commit()

    conn.close()

    # Group by category
    products_by_category = {}
    for product in products:
        cat = product['category']
        if cat not in products_by_category:
            products_by_category[cat] = []
        products_by_category[cat].append(product)

    return render_template(
        'admin/inventory.html',
        products_by_category=products_by_category,
        selected_date=selected_date,
        current_year=datetime.now().year
    )

@app.route('/admin/activity-logs')
def view_logs():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT log_id, full_name, action, timestamp
        FROM activity_log
        ORDER BY timestamp DESC
    """)
    logs = cursor.fetchall()
    conn.close()
    return render_template('admin/view_logs.html', logs=logs)

@app.route('/admin/admin_today_sales', methods=['GET'])
def admin_today_sales():
    from datetime import date
    selected_date = request.args.get('date', date.today().strftime('%Y-%m-%d'))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT order_id, customer_name, date_created, total_price
        FROM orders
        WHERE DATE(date_created) = %s
        ORDER BY date_created DESC
    """, (selected_date,))
    sales = cursor.fetchall()
    conn.close()

    return render_template(
        'admin/dashboard.html',
        sales=sales,
        selected_date=selected_date,
        current_year=date.today().year
    )

@app.route('/warehouse_dashboard')
def warehouse_dashboard():
    now = datetime.now()
    is_am = now.hour < 12

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    if is_am:
        # AM stock check
        cursor.execute("""
            SELECT COUNT(*) as restock_count
            FROM products p
            LEFT JOIN (
                SELECT product_id, am FROM inventorylog WHERE DATE(date) = CURDATE()
            ) il ON p.product_id = il.product_id
            WHERE p.reorder_level IS NOT NULL AND COALESCE(il.am, 0) <= p.reorder_level
        """)
    else:
        # PM stock check
        cursor.execute("""
            SELECT COUNT(*) as restock_count
            FROM products p
            LEFT JOIN (
                SELECT product_id, pm FROM inventorylog WHERE DATE(date) = CURDATE()
            ) il ON p.product_id = il.product_id
            WHERE p.reorder_level IS NOT NULL AND COALESCE(il.pm, 0) <= p.reorder_level
        """)

    restock_count = cursor.fetchone()['restock_count']

    # Build base query (do not execute yet) and read optional filters from request
    query = """
        SELECT il.log_id, p.product_name, p.category, il.date,
               il.am, il.pm
        FROM inventorylog il
        JOIN products p ON il.product_id = p.product_id
        WHERE 1=1
    """
    # optional search and date filters from request args
    search_query = request.args.get('search_query', '').strip()
    # default to today's date string in YYYY-MM-DD format if not provided
    date_filter = request.args.get('date_filter', datetime.now().date().strftime('%Y-%m-%d'))
    params = []

    if search_query:
        query += " AND p.product_name LIKE %s"
        params.append(f"%{search_query}%")

    # Always filter by date (today by default)
    query += " AND DATE(il.date) = %s"
    params.append(date_filter)

    query += " ORDER BY il.date DESC"

    cursor.execute(query, tuple(params))
    logs = cursor.fetchall()

    # Low stock check
    cursor.execute("SELECT COUNT(*) AS restock_count FROM products WHERE quantity <= reorder_level")
    restock_count = cursor.fetchone()['restock_count']

    cursor.execute("""
        SELECT COUNT(*) AS restock_count
        FROM products p
        LEFT JOIN (
            SELECT product_id, pm
            FROM inventorylog
            WHERE DATE(date) = %s
        ) il ON p.product_id = il.product_id
        WHERE p.reorder_level IS NOT NULL AND COALESCE(il.pm, 0) <= p.reorder_level
    """, (date_filter,))
    restock_count = cursor.fetchone()['restock_count']

    conn.close()

    # Get products expiring within the next 7 days using a direct SQL query
    soon = datetime.today().date() + timedelta(days=7)
    conn2 = get_db_connection()
    cursor2 = conn2.cursor(dictionary=True)
    cursor2.execute("""
        SELECT product_id, product_name, expiry_date
        FROM products
        WHERE expiry_date IS NOT NULL
          AND expiry_date <= %s
          AND expiry_date >= %s
        ORDER BY expiry_date ASC
    """, (soon, datetime.today().date()))
    expiring_products = cursor2.fetchall()
    cursor2.close()
    conn2.close()

    return render_template(
        'warehouse/dashboard.html',
        expiring_products=expiring_products,
        logs=logs,
        restock_count=restock_count,
        search_query=search_query,
        date_filter=date_filter,
        current_year=datetime.now().year
    )

@app.route('/warehouse/inventory', methods=['GET', 'POST'])
def warehouse_inventory():
    if request.method == 'POST':
        product_id = request.form.get('product_id')
        am = request.form.get('am', 0)
        pm = request.form.get('pm', 0)
        today = datetime.now().date()

        conn = get_db_connection()
        cursor = conn.cursor()

        # Insert or update inventory log
        cursor.execute("""
            INSERT INTO inventorylog (product_id, date, am, pm)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
            am = VALUES(am),
            pm = VALUES(pm)
        """, (product_id, today, am, pm))

        conn.commit()
        conn.close()

        flash('Inventory updated successfully!', 'success')
        return redirect(url_for('warehouse_inventory'))

    # Fetch products and group by category
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT p.category, p.product_id, p.product_name, p.price, p.kilo_per_unit,
               COALESCE(il.am, 0) AS am, COALESCE(il.pm, 0) AS pm
        FROM products p
        LEFT JOIN (
            SELECT product_id, am, pm
            FROM inventorylog
            WHERE DATE(date) = CURDATE()
        ) il ON p.product_id = il.product_id
        ORDER BY p.category, p.product_name
    """)
    products = cursor.fetchall()

    products_by_category = {}
    for product in products:
        category = product['category']
        if category not in products_by_category:
            products_by_category[category] = []
        products_by_category[category].append(product)

    conn.close()
    return render_template(
        'warehouse/manage_inventory.html',
        products_by_category=products_by_category,
        current_year=datetime.now().year
    )


@app.route('/warehouse/restock_alerts')
def restock_alerts():
    today = datetime.now().date()
    now = datetime.now()
    is_am = now.hour < 12  # Check if it's AM or PM

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    stock_column = 'am' if is_am else 'pm'  # Use AM stock in the morning, PM stock in the afternoon

    cursor.execute(f"""
        SELECT p.product_id, p.product_name, p.category, p.reorder_level, 
               COALESCE(il.{stock_column}, 0) AS current_stock
        FROM products p
        LEFT JOIN (
            SELECT product_id, {stock_column}
            FROM inventorylog
            WHERE DATE(date) = %s
        ) il ON p.product_id = il.product_id
        WHERE p.reorder_level IS NOT NULL 
          AND COALESCE(il.{stock_column}, 0) <= p.reorder_level
        ORDER BY current_stock ASC
    """, (today,))
    restocks = cursor.fetchall()
    conn.close()

    return render_template(
        'warehouse/restock.html',
        restocks=restocks,
        current_year=datetime.now().year
    )

@app.route('/warehouse/add_product', methods=['POST'])
def add_product():
    category = request.form['category']
    product_name = request.form['product_name']
    price = request.form['price']
    kilo_per_unit = request.form['kilo_per_unit']
    # Quantity and reorder_level fields were removed from the modal form.
    # Default them to 0 when not provided to keep backward compatibility.
    try:
        quantity = int(request.form.get('quantity', 0))
    except (ValueError, TypeError):
        quantity = 0

    # If reorder_level is not provided or blank, try to pick a sensible default
    # based on existing products in the same category (mode or rounded average).
    rl_raw = request.form.get('reorder_level')
    if rl_raw is None or rl_raw == '':
        # If reorder_level is not provided, default to None so the DB can store NULL,
        # or change this to a numeric default like 0 if preferred.
        reorder_level = None
    else:
        try:
            reorder_level = int(rl_raw)
        except (ValueError, TypeError):
            reorder_level = None

    expiry_date_str = request.form.get('expiry_date')
    expiry_date = datetime.strptime(expiry_date_str, '%Y-%m-%d').date() if expiry_date_str else None

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO products (category, product_name, price, kilo_per_unit, quantity, reorder_level, expiry_date)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (category, product_name, price, kilo_per_unit, quantity, reorder_level, expiry_date))
    conn.commit()

    # Log the action to activity_log (warehouse staff full name)
    full_name = get_full_name(session.get('user_id'))
    log_cursor = conn.cursor()
    log_cursor.execute(
        "INSERT INTO activity_log (full_name, action) VALUES (%s, %s)",
        (full_name, f"Added product '{product_name}' in category '{category}'")
    )
    conn.commit()
    conn.close()

    flash("Product added successfully!", "success")
    return redirect(url_for('warehouse_dashboard'))

@app.route('/update_warehouse_account', methods=['POST'])
def update_warehouse_account():
    email = request.form['email']
    password = request.form['password']
    # Update warehouse user in the database here

    conn = get_db_connection()
    cursor = conn.cursor()

    # Log the action (warehouse staff full name)
    full_name = get_full_name(session.get('user_id'))
    cursor.execute(
        "INSERT INTO activity_log (full_name, action) VALUES (%s, %s)",
        (full_name, "Updated warehouse account")
    )
    conn.commit()
    conn.close()

    flash("Account updated successfully!", "success")
    return redirect(url_for('warehouse_dashboard'))


@app.route('/warehouse/delete_product', methods=['POST'])
def delete_product():
    # Ensure user has appropriate role? (Optional)
    product_id = request.form.get('product_id')
    if not product_id:
        flash('Missing product id.', 'error')
        return redirect(url_for('warehouse_inventory'))

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        # Get product name for logging
        cursor.execute('SELECT product_name FROM products WHERE product_id = %s', (product_id,))
        row = cursor.fetchone()
        product_name = row[0] if row else 'Unknown'

        # First remove dependent inventorylog rows to avoid FK constraint errors
        # (Safer than forcing DB schema changes here)
        cursor.execute('DELETE FROM inventorylog WHERE product_id = %s', (product_id,))

        # Now delete the product
        cursor.execute('DELETE FROM products WHERE product_id = %s', (product_id,))

        # Log the deletion
        full_name = get_full_name(session.get('user_id'))
        cursor.execute(
            "INSERT INTO activity_log (full_name, action) VALUES (%s, %s)",
            (full_name, f"Deleted product '{product_name}' (id={product_id})")
        )

        conn.commit()
        flash('Product and related inventory logs deleted successfully.', 'success')
    except Exception as e:
        # Rollback and return a helpful message
        try:
            conn.rollback()
        except Exception:
            pass
        # If it's a MySQL integrity related error, show a clearer message
        err_msg = str(e)
        if 'foreign key constraint' in err_msg.lower() or '1451' in err_msg:
            flash('Cannot delete product because related records exist. Please clear related inventory logs or contact admin.', 'error')
        else:
            flash('An error occurred while deleting the product: ' + err_msg, 'error')
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return redirect(url_for('warehouse_inventory'))

@app.route('/secretary/dashboard')
def secretary_dashboard():
    if 'role' not in session or session['role'] != 'secretary':
        flash("Access denied.", "error")
        return redirect(url_for('login'))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    # Count pending deliveries (adjust query to your actual deliveries table & status field)
    cursor.execute("""
        SELECT COUNT(*) AS pending_count
        FROM orders
        WHERE status = 'pending'
    """)
    pending_deliveries = cursor.fetchone()['pending_count']

    # Count completed deliveries today
    cursor.execute("""
        SELECT COUNT(*) AS completed_today
        FROM orders
        WHERE status = 'completed' AND DATE(date_created) = CURDATE()
    """)
    completed_deliveries_today = cursor.fetchone()['completed_today']

    conn.close()

    return render_template(
        'secretary/dashboard.html',
        pending_deliveries=pending_deliveries,
        completed_deliveries_today=completed_deliveries_today,
        current_year=datetime.now().year
    )

@app.route('/secretary/pending_deliveries')
def view_pending_deliveries():
    if 'role' not in session or session['role'] != 'secretary':
        flash("Access denied.", "error")
        return redirect(url_for('login'))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    # Join with users table to get full name of assigned driver
    cursor.execute("""
        SELECT o.*, u.full_name AS driver_full_name
        FROM orders o
        LEFT JOIN users u ON o.assigned_driver = u.user_id
        WHERE o.status = 'pending' AND o.assigned_driver IS NOT NULL AND o.assigned_driver != ''
    """)
    deliveries = cursor.fetchall()

    # Attach order items to each delivery
    for delivery in deliveries:
        cursor.execute("""
            SELECT oi.*, p.product_name 
            FROM order_items oi 
            JOIN products p ON oi.product_id = p.product_id 
            WHERE oi.order_id = %s
        """, (delivery['order_id'],))
        delivery['order_items'] = cursor.fetchall()

    conn.close()
    return render_template('secretary/pending_deliveries.html', deliveries=deliveries)

@app.route('/secretary/completed_deliveries')
def view_completed_deliveries():
    completed_deliveries = get_completed_deliveries()
    return render_template(
        'secretary/completed_deliveries.html',
        completed_deliveries=completed_deliveries,
        current_year=datetime.now().year
    )

def get_completed_deliveries():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT d.*, u.full_name AS driver_full_name
        FROM deliveries d
        LEFT JOIN users u ON d.assigned_driver = u.user_id
        WHERE d.status = 'delivered'
        ORDER BY d.date_created DESC
    """)
    deliveries = cursor.fetchall()
    # Attach order items to each delivery (from orders/order_items)
    for delivery in deliveries:
        cursor.execute("""
            SELECT oi.*, p.product_name 
            FROM order_items oi 
            JOIN products p ON oi.product_id = p.product_id 
            WHERE oi.order_id = %s
        """, (delivery['order_id'],))
        delivery['order_items'] = cursor.fetchall()
    conn.close()
    return deliveries

@app.route('/secretary/create_customer', methods=['GET', 'POST'])
def create_customer():
    if 'role' not in session or session['role'] != 'secretary':
        return redirect(url_for('login'))

    if request.method == 'POST':
        customer_name = request.form['customer_name']
        contact_number = request.form['contact_number']
        address = request.form['address']

        conn = get_db_connection()
        cursor = conn.cursor()

        # Insert new customer
        cursor.execute("""
            INSERT INTO customers (customer_name, contact_number, address)
            VALUES (%s, %s, %s)
        """, (customer_name, contact_number, address))

        # Log the activity
        activity = f"Added new customer: {customer_name}"
        cursor.execute("""
            INSERT INTO activity_log (full_name, action, timestamp)
            VALUES (%s, %s, NOW())
        """, (get_full_name(session.get('user_id')), activity))

        conn.commit()
        conn.close()

        flash('Customer added successfully!', 'success')
        return redirect(url_for('secretary_dashboard'))

    return render_template('secretary/create_customer.html', current_year=datetime.now().year)

# Added route to satisfy templates that call url_for('manage_customers')
@app.route('/secretary/manage_customers')
def manage_customers():
    if 'role' not in session or session['role'] != 'secretary':
        flash("Access denied.", "error")
        return redirect(url_for('login'))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT customer_id, customer_name, contact_number, address, created_at FROM customers ORDER BY created_at DESC")
    customers = cursor.fetchall()
    conn.close()

    return render_template('secretary/manage_customers.html', customers=customers, current_year=datetime.now().year)

@app.route('/secretary/create_order', methods=['GET', 'POST'])
def create_order():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    # Get customers, drivers, categories, and products (unchanged)
    cursor.execute("SELECT * FROM customers")
    customers = cursor.fetchall()

    cursor.execute("SELECT user_id, full_name FROM users WHERE role = 'delivery_driver'")
    drivers = cursor.fetchall()

    cursor.execute("SELECT DISTINCT category FROM products")
    categories = [row['category'] for row in cursor.fetchall()]

    cursor.execute("""
        SELECT 
            p.product_id,
            p.product_name,
            p.category,
            p.price,
            p.kilo_per_unit,
            COALESCE(il.am, 0) AS am_stock,
            COALESCE(il.pm, 0) AS pm_stock
        FROM products p
        LEFT JOIN (
            SELECT product_id, am, pm
            FROM inventorylog
            WHERE date = CURDATE()
        ) il ON p.product_id = il.product_id
    """)
    product_rows = cursor.fetchall()

    products = []
    for row in product_rows:
        products.append({
            'product_id': row['product_id'],
            'product_name': row['product_name'],
            'category': row['category'],
            'price': row['price'],
            'kilo_per_unit': row['kilo_per_unit'],
            'pm_stock': row['pm_stock'],
            'am_stock': row['am_stock'],
        })

    current_time = datetime.now().strftime('%Y-%m-%dT%H:%M')
    current_year = datetime.now().year

    if request.method == 'POST':
        customer_name = request.form.get('customer_name')
        address = request.form.get('address')
        assigned_driver = request.form.get('assigned_driver')
        status = request.form.get('status')
        created_at = request.form.get('created_at') or datetime.now().strftime('%Y-%m-%dT%H:%M')
        total_price = request.form.get('total_price') or 0
        product_ids = request.form.getlist('product_id[]')
        quantities = request.form.getlist('quantity[]')
        kilos = request.form.getlist('kilos[]')

        # Insert order
        cursor.execute("""
            INSERT INTO orders (customer_name, address, assigned_driver, status, date_created, total_price)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (customer_name, address, assigned_driver, status, created_at, total_price))
        order_id = cursor.lastrowid

        # Insert order items
        for pid, qty, kilo in zip(product_ids, quantities, kilos):
            cursor.execute("""
                INSERT INTO order_items (order_id, product_id, quantity, kilos)
                VALUES (%s, %s, %s, %s)
            """, (order_id, pid, qty, kilo))

        # Deduct inventory stock
        for pid, qty in zip(product_ids, quantities):
            stock_type = 'am' if datetime.now().hour < 12 else 'pm'  # Determine AM or PM stock
            cursor.execute(f"""
                UPDATE inventorylog
                SET {stock_type} = {stock_type} - %s
                WHERE product_id = %s AND date = CURDATE()
            """, (qty, pid))

        # Insert into sales table
        cursor.execute("""
            INSERT INTO sales (order_id, customer_name, total_amount, sale_date)
            VALUES (%s, %s, %s, %s)
        """, (order_id, customer_name, total_price, datetime.now().strftime('%Y-%m-%d')))

        conn.commit()
        conn.close()
        flash('Order created and recorded in sales!', 'success')
        return redirect(url_for('secretary_dashboard'))

    # GET: render the create order page
    conn.close()
    return render_template(
        'secretary/create_order.html',
        customers=customers,
        drivers=drivers,
        categories=categories,
        products=products,
        current_time=current_time,
        current_year=current_year
    )
    

@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    flash('Logged out successfully!', 'success')
    return redirect(url_for('home'))

@app.route('/warehouse/edit_product', methods=['POST'])
def edit_product():
    conn = get_db_connection()
    cursor = conn.cursor()
    product_id = request.form['product_id']
    product_name = request.form['product_name']
    price = request.form['price']
    kilo_per_unit = request.form['kilo_per_unit']
    cursor.execute("""
        UPDATE products
        SET product_name=%s, price=%s, kilo_per_unit=%s
        WHERE product_id=%s
    """, (product_name, price, kilo_per_unit, product_id))
    conn.commit()
    
    # Log the activity
    activity = f"Edited product: {product_name} (ID: {product_id})"
    cursor.execute("""
        INSERT INTO activity_log (full_name, action, timestamp)
        VALUES (%s, %s, NOW())
    """, (get_full_name(session.get('user_id')), activity))
    conn.commit()
    conn.close()

    flash('Product updated successfully!', 'success')
    return redirect(url_for('warehouse_inventory'))

@app.route('/warehouse/edit_product_inventory', methods=['POST'])
def edit_product_inventory():
    conn = get_db_connection()
    cursor = conn.cursor()
    product_id = request.form['product_id']
    am = request.form['am']
    pm = request.form['pm']
    cursor.execute("""
        UPDATE products
        SET am=%s, pm=%s
        WHERE product_id=%s
    """, (am, pm, product_id))
    conn.commit()
    flash('Inventory updated successfully!', 'success')
    # Log the activity
    activity = f"Edited inventory for product ID: {product_id}"
    cursor.execute("""
        INSERT INTO activity_log (full_name, action, timestamp)
        VALUES (%s, %s, NOW())
    """, (get_full_name(session.get('user_id')), activity))
    conn.commit()
    conn.close()
    return redirect(url_for('warehouse_inventory'))

@app.route('/api/deliveries')
def get_deliveries():
    if 'role' not in session or session['role'] != 'delivery_driver':
        return {'success': False, 'message': 'Unauthorized'}, 401

    user_id = session.get('user_id')
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        
        # Get today's deliveries for the logged-in driver
        today = datetime.now().date()
        cursor.execute("""
            SELECT o.order_id, o.customer_name, o.address, o.status, o.date_created,
                   GROUP_CONCAT(CONCAT(oi.quantity, 'x ', p.product_name) SEPARATOR ', ') as products
            FROM orders o
            LEFT JOIN order_items oi ON o.order_id = oi.order_id
            LEFT JOIN products p ON oi.product_id = p.product_id
            WHERE o.assigned_driver = %s 
            AND DATE(o.date_created) = %s 
            GROUP BY o.order_id
            ORDER BY o.date_created ASC
        """, (user_id, today))
        deliveries = cursor.fetchall()
        
        # Add sample coordinates for demo
        # In real app, these would come from the database
        import random
        for delivery in deliveries:
            delivery['latitude'] = 14.1 + random.uniform(-0.1, 0.1)
            delivery['longitude'] = 121.4 + random.uniform(-0.1, 0.1)
        
        return {'success': True, 'deliveries': deliveries}
    except Exception as e:
        return {'success': False, 'message': str(e)}, 500
    finally:
        if 'conn' in locals():
            conn.close()

@app.route('/api/driver/stats')
def get_driver_stats():
    if 'role' not in session or session['role'] != 'delivery_driver':
        return {'success': False, 'message': 'Unauthorized'}, 401

    user_id = session.get('user_id')
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        today = datetime.now().date()

        # Get assigned deliveries count
        cursor.execute("""
            SELECT COUNT(*) as count 
            FROM orders 
            WHERE assigned_driver = %s AND DATE(date_created) = %s
        """, (user_id, today))
        assigned = cursor.fetchone()['count']

        # Get completed deliveries count
        cursor.execute("""
            SELECT COUNT(*) as count 
            FROM orders 
            WHERE assigned_driver = %s 
            AND DATE(date_created) = %s 
            AND status = 'delivered'
        """, (user_id, today))
        completed = cursor.fetchone()['count']

        # Sample distance and fuel calculations
        # In a real app, these would be calculated based on actual route data
        distance = completed * 5  # Assume 5km per delivery
        fuel_used = f"{distance * 0.1:.1f}L"  # Assume 0.1L per km

        stats = {
            'assigned': assigned,
            'completed': completed,
            'distance': distance,
            'fuel_used': fuel_used
        }
        
        return {'success': True, 'stats': stats}
    except Exception as e:
        return {'success': False, 'message': str(e)}, 500
    finally:
        if 'conn' in locals():
            conn.close()

@app.route('/delivery/dashboard')
def delivery_dashboard():
    if 'role' not in session or session['role'] != 'delivery_driver':
        return redirect(url_for('login'))

    user_id = session.get('user_id')
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    today = datetime.now().date()

    # Assigned deliveries (pending/processing)
    cursor.execute("""
        SELECT o.order_id, o.customer_name, o.address, o.status
        FROM orders o
        WHERE o.assigned_driver = %s AND DATE(o.date_created) = %s AND o.status IN ('pending', 'processing')
        ORDER BY o.date_created ASC
    """, (user_id, today))
    assigned_deliveries = cursor.fetchall()

    # Completed deliveries (from deliveries table)
    cursor.execute("""
        SELECT d.order_id, d.customer_name, d.address, d.status, d.completed_at
        FROM deliveries d
        WHERE d.assigned_driver = %s AND d.status = 'completed'
        ORDER BY d.completed_at DESC
    """, (user_id,))
    completed_deliveries = cursor.fetchall()

    conn.close()

    return render_template(
        'delivery/dashboard.html',
        assigned_deliveries=assigned_deliveries,
        completed_deliveries=completed_deliveries,
        google_maps_api_key=API_KEY,
        current_year=datetime.now().year
    )
@app.route('/delivery-route')
def delivery_route():
    return render_template('delivery/delivery_route.html')

@app.route('/delivery/assigned_deliveries')
def assigned_deliveries():
    if 'role' not in session or session['role'] != 'delivery_driver':
        flash("Access denied.", "error")
        return redirect(url_for('login'))

    user_id = session.get('user_id')
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT full_name FROM users WHERE user_id = %s", (user_id,))
    user = cursor.fetchone()
    driver_name = user['full_name'] if user else ''

    # JOIN to get full name for display
    cursor.execute("""
        SELECT o.*, u.full_name AS driver_full_name
        FROM orders o
        LEFT JOIN users u ON o.assigned_driver = u.user_id
        WHERE o.assigned_driver = %s AND o.status = 'pending'
        ORDER BY o.date_created DESC
    """, (user_id,))
    deliveries = cursor.fetchall()

    # Attach order items to each delivery
    for delivery in deliveries:
        cursor.execute("""
            SELECT oi.*, p.product_name 
            FROM order_items oi 
            JOIN products p ON oi.product_id = p.product_id 
            WHERE oi.order_id = %s
        """, (delivery['order_id'],))
        delivery['order_items'] = cursor.fetchall()

    today_date = date.today().strftime('%B %d, %Y')
    conn.close()

    return render_template(
        'delivery/assigned_delivery.html',
        deliveries=deliveries,
        driver_name=driver_name,
        today_date=today_date,
        current_year=date.today().year
    )

@app.route('/delivery/completed_deliveries')
def completed_deliveries():
    if 'role' not in session or session['role'] != 'delivery_driver':
        return redirect(url_for('login'))

    user_id = session.get('user_id')
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT d.*, u.full_name AS driver_full_name
        FROM deliveries d
        LEFT JOIN users u ON d.assigned_driver = u.user_id
        WHERE d.assigned_driver = %s AND d.status = 'completed'
        ORDER BY d.completed_at DESC
    """, (user_id,))
    completed_deliveries = cursor.fetchall()
    conn.close()
    return render_template(
        'delivery/completed_delieveries.html',
        completed_deliveries=completed_deliveries,
        current_year=datetime.now().year
    )

@app.route('/delivery/update_status/<int:order_id>', methods=['POST'])
def update_delivery_status(order_id):
    new_status = request.form['status']
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE orders SET status = %s WHERE order_id = %s", (new_status, order_id))
    conn.commit()
    flash('Delivery status updated!', 'success')
    # Log the activity
    activity = f"Updated delivery status for order ID: {order_id} to {new_status}"
    cursor.execute("""
        INSERT INTO activity_log (full_name, action, timestamp)
        VALUES (%s, %s, NOW())
    """, (get_full_name(session.get('user_id')), activity))
    conn.commit()
    conn.close()
    return redirect(url_for('assigned_deliveries'))

@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form['email']
        # Check if email exists in your users table
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM users WHERE email = %s", (email,))
        user = cursor.fetchone()
        conn.close()
        if user:
            # Generate a token (for example, using itsdangerous)
            from itsdangerous import URLSafeTimedSerializer
            s = URLSafeTimedSerializer(app.secret_key)
            token = s.dumps(email, salt='password-reset-salt')
            reset_link = url_for('reset_password', token=token, _external=True)
            # Send email (see below)
            send_reset_email(email, reset_link)
            flash('Password reset link sent to your email!', 'success')
        else:
            flash('Email not found.', 'error')
    return render_template('forgot_password.html')

@app.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature
    s = URLSafeTimedSerializer(app.secret_key)
    try:
        email = s.loads(token, salt='password-reset-salt', max_age=3600)  # 1 hour expiry
    except (SignatureExpired, BadSignature):
        flash('The reset link is invalid or expired.', 'error')
        return redirect(url_for('login'))
    if request.method == 'POST':
        new_password = request.form['password']
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET password = %s WHERE email = %s", (new_password, email))
        conn.commit()
        conn.close()
        flash('Password has been reset!', 'success')
        return redirect(url_for('login'))
    return render_template('reset_password.html')

def send_reset_email(to_email, reset_link):
    msg = Message('Password Reset Request',
                  sender='your_email@gmail.com',
                  recipients=[to_email])
    msg.body = f'Click the link to reset your password: {reset_link}'
    mail.send(msg)

# Geocode deliveries without coordinates (run once or as needed)
# Requires: pip install mysql-connector-python requests
# Comment out or remove after initial run to prevent overwriting
"""
cnx = mysql.connector.connect(**DB_CONFIG)
cur = cnx.cursor(dictionary=True)

cur.execute("SELECT delivery_id, address FROM deliveries WHERE (latitude IS NULL OR longitude IS NULL) AND address IS NOT NULL")
rows = cur.fetchall()
for row in rows:
    delivery_id = row['delivery_id']
    addr = row['address']
    lat, lng = geocode(addr)
    if lat and lng:
        cur2 = cnx.cursor()
        cur2.execute("UPDATE deliveries SET latitude=%s, longitude=%s WHERE delivery_id=%s", (lat, lng, delivery_id))
        cnx.commit()
        cur2.close()
        print(f"Updated {delivery_id} -> {lat},{lng}")
    else:
        print(f"Geocode failed for {delivery_id}: {addr}")
    time.sleep(0.1)  # polite pacing to avoid quotas

cur.close()
cnx.close()
"""

today_date = date.today().strftime('%B %d, %Y')



@app.route('/api/complete_delivery/<int:order_id>', methods=['POST'])
def api_complete_delivery(order_id):
    data = request.get_json()
    delivery_notes = data.get('delivery_notes')
    proof_photos = data.get('proof_photos', [])

    # Save proof_photos (base64) and notes to DB or filesystem as needed
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE orders SET status = %s, delivery_notes = %s WHERE order_id = %s",
        ('delivered', delivery_notes, order_id)
    )
    # Example: Save proof_photos filenames to another table if needed
    # for photo in proof_photos:
    #     save_photo(photo, order_id)  # Implement this function

    conn.commit()
    conn.close()

    return jsonify({'success': True, 'message': 'Delivery marked as completed!'})

@app.route('/delivery/mark_completed/<int:order_id>', methods=['POST'])
def mark_completed(order_id):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    # Get order details
    cursor.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,))
    order = cursor.fetchone()

    if order:
        # Insert into deliveries table
        cursor2 = conn.cursor()
        cursor2.execute("""
            INSERT INTO deliveries (
                order_id, customer_name, address, assigned_driver, status, date_created, completed_at
            ) VALUES (%s, %s, %s, %s, %s, %s, NOW())
        """, (
            order['order_id'],
            order['customer_name'],
            order['address'],
            order['assigned_driver'],
            'completed',  # status
            order['date_created']
        ))
        # Optional: delete from orders table
        cursor.execute("DELETE FROM orders WHERE order_id = %s", (order_id,))
        conn.commit()
        cursor2.close()

    conn.close()
    return redirect(url_for('delivery_dashboard'))

# Sort and get top 3 products (only if a DataFrame 'df' exists)
chart_top3_html = ""
try:
    # Safely obtain df from globals or locals to avoid NameError
    df_obj = globals().get('df', locals().get('df', None))
    if df_obj is not None:
        import plotly.express as px
        # Ensure the expected columns exist before sorting/plotting
        cols = getattr(df_obj, 'columns', None)
        if cols is not None and {'quantity', 'product'}.issubset(set(cols)):
            top3 = df_obj.sort_values('quantity', ascending=False).head(3)
            fig = px.bar(top3, x='product', y='quantity')
            chart_top3_html = pyo.plot(fig, output_type='div', include_plotlyjs=False)
except Exception:
    # Fail silently and keep chart HTML empty if anything goes wrong
    chart_top3_html = ""

@app.route('/admin/upload_csv', methods=['POST'])
def upload_csv():
    # You can add your CSV processing logic here
    from flask import request, redirect, url_for, flash
    file = request.files.get('csv_file')
    if file and file.filename.endswith('.csv'):
        # Process the CSV file as needed
        flash('CSV file uploaded successfully!', 'success')
    else:
        flash('Please upload a valid CSV file.', 'error')
    return redirect(url_for('admin_dashboard'))

@app.route('/secretary/download_customers')
def download_customers():
    if 'role' not in session or session['role'] != 'secretary':
        flash("Access denied.", "error")
        return redirect(url_for('login'))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT customer_id, customer_name, contact_number, address, created_at FROM customers ORDER BY created_at DESC")
    customers = cursor.fetchall()
    conn.close()

    # Build CSV in memory
    si = io.StringIO()
    writer = csv.writer(si)
    writer.writerow(['Customer ID', 'Customer Name', 'Contact Number', 'Address', 'Created At'])
    for c in customers:
        created = c.get('created_at')
        if hasattr(created, 'strftime'):
            created = created.strftime('%Y-%m-%d %H:%M:%S')
        writer.writerow([c.get('customer_id'), c.get('customer_name'), c.get('contact_number'), c.get('address'), created])

    output = si.getvalue()
    si.close()

    from flask import make_response
    response = make_response(output)
    response.headers['Content-Disposition'] = 'attachment; filename=customers.csv'
    response.headers['Content-Type'] = 'text/csv; charset=utf-8'
    return response

@app.route('/update_stock', methods=['POST'])
def update_stock():
    product_id = request.form['product_id']
    stock_type = 'am' if datetime.now().hour < 12 else 'pm'  # Determine AM or PM
    quantity = int(request.form['quantity'])
    today = datetime.now().date()

    conn = get_db_connection()
    cursor = conn.cursor()

    # Update the correct stock column
    cursor.execute(f"""
        INSERT INTO inventorylog (product_id, date, {stock_type})
        VALUES (%s, %s, %s)
        ON DUPLICATE KEY UPDATE {stock_type} = {stock_type} + VALUES({stock_type})
    """, (product_id, today, quantity))

    conn.commit()
    cursor.close()
    conn.close()

    flash('Stock updated successfully!', 'success')
    return redirect(url_for('restock_alerts'))

def shift_pm_to_am():
    conn = get_db_connection()
    cursor = conn.cursor()

    yesterday = (datetime.now() - timedelta(days=1)).date()
    today = datetime.now().date()

    # Copy PM stock from yesterday to AM stock for today
    cursor.execute("""
        INSERT INTO inventorylog (product_id, date, am)
        SELECT product_id, %s, pm
        FROM inventorylog
        WHERE date = %s
    """, (today, yesterday))

    conn.commit()
    cursor.close()
    conn.close()

@app.route('/secretary/download_completed_deliveries')
def download_completed_deliveries():
    # Example logic to generate a CSV file for completed deliveries
    completed_deliveries = get_completed_deliveries()
    si = io.StringIO()
    writer = csv.writer(si)
    writer.writerow(['Order ID', 'Customer', 'Address', 'Date Completed', 'Driver', 'Total Price'])
    for delivery in completed_deliveries:
        writer.writerow([
            delivery['order_id'],
            delivery['customer_name'],
            delivery['address'],
            delivery['date_created'],
            delivery.get('driver_full_name', 'N/A'),
            delivery['total_price']
        ])
    output = si.getvalue()
    si.close()

    from flask import make_response
    response = make_response(output)
    response.headers['Content-Disposition'] = 'attachment; filename=completed_deliveries.csv'
    response.headers['Content-Type'] = 'text/csv; charset=utf-8'
    return response

if __name__ == '__main__':
    app.run(debug=True)