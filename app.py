import streamlit as st
import requests
import pandas as pd
import gspread
from datetime import datetime, timedelta
import time

# --- CONFIGURATION ---
LAB_MEMBERS = ["Select User...", "Albert", "Shinsuke", "Jaeson", "Brian"]

MEMBER_COLORS = {
    "Albert": "#3498db",   # Blue
    "Shinsuke": "#2ecc71", # Green
    "Jaeson": "#e67e22",   # Orange
    "Brian": "#e74c3c"     # Red
}

# --- GOOGLE SHEETS CONNECTION (CACHED) ---
def get_db_connection():
    """Establishes connection to Google Sheets using Streamlit Secrets."""
    try:
        gc = gspread.service_account_from_dict(st.secrets["gcp_service_account"])
        sh = gc.open_by_url(st.secrets["private_gsheets_url"])
        return sh
    except Exception as e:
        st.error(f"Failed to connect to Google Sheets: {e}")
        st.stop()

# --- CACHED DATA FETCHING ---
# CRITICAL FIX: This decorator tells Streamlit "Don't run this function if you ran it 
# less than 60 seconds ago. Just give me the result you saved."
@st.cache_data(ttl=60)
def load_all_data_from_sheets():
    sh = get_db_connection()
    
    # We fetch all 3 sheets at once to minimize connection overhead
    try:
        papers_data = sh.worksheet("papers").get_all_records()
        interest_data = sh.worksheet("interest").get_all_records()
        seen_data = sh.worksheet("seen").get_all_records()
        return papers_data, interest_data, seen_data
    except gspread.exceptions.WorksheetNotFound:
        # If sheets don't exist yet (first run), return empty lists so init_db can handle it
        return [], [], []
    except Exception as e:
        # If quota exceeded, wait a sec and return empty (or handle gracefully)
        st.warning(f"High traffic (Quota Limit). Please wait 10s and refresh. Error: {e}")
        return [], [], []

def init_db():
    """Checks if necessary worksheets exist."""
    sh = get_db_connection()
    try:
        sh.worksheet("papers")
    except:
        ws = sh.add_worksheet(title="papers", rows=1000, cols=7)
        ws.append_row(["doi", "title", "authors", "abstract", "link", "category", "date"])
    try:
        sh.worksheet("interest")
    except:
        ws = sh.add_worksheet(title="interest", rows=1000, cols=3)
        ws.append_row(["doi", "user", "timestamp"])
    try:
        sh.worksheet("seen")
    except:
        ws = sh.add_worksheet(title="seen", rows=1000, cols=2)
        ws.append_row(["doi", "user"])

def mark_as_seen(doi, user):
    """Writes to DB and clears cache so the UI updates instantly."""
    sh = get_db_connection()
    ws = sh.worksheet("seen")
    ws.append_row([doi, user])
    # IMPORTANT: Clear cache so the user sees the paper vanish immediately
    st.cache_data.clear()

def batch_update_votes(user, selected_dois, all_displayed_dois):
    sh = get_db_connection()
    ws = sh.worksheet("interest")
    
    # We must read fresh data here to avoid overwriting recent votes
    data = ws.get_all_records()
    df = pd.DataFrame(data)
    
    if df.empty:
        df = pd.DataFrame(columns=["doi", "user", "timestamp"])
    else:
        df['doi'] = df['doi'].astype(str)
        df['user'] = df['user'].astype(str)

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    changes_count = 0
    
    current_user_votes = df[df['user'] == user]['doi'].tolist()
    rows_to_add = []
    modified = False
    
    for doi in all_displayed_dois:
        is_checked = doi in selected_dois
        already_voted = doi in current_user_votes
        
        if is_checked and not already_voted:
            rows_to_add.append({"doi": doi, "user": user, "timestamp": timestamp})
            modified = True
            changes_count += 1
        elif not is_checked and already_voted:
            df = df[~((df['doi'] == doi) & (df['user'] == user))]
            modified = True
            changes_count += 1

    if modified:
        if rows_to_add:
            df = pd.concat([df, pd.DataFrame(rows_to_add)], ignore_index=True)
        ws.clear()
        ws.append_row(["doi", "user", "timestamp"])
        if not df.empty:
            ws.append_rows(df.values.tolist())
        
        # IMPORTANT: Clear cache so the new votes show up immediately
        st.cache_data.clear()
            
    return changes_count

# --- DATA PROCESSING (Uses Cached Data) ---
def get_shortlist_data(current_user):
    # Load from Cache
    papers_data, interest_data, _ = load_all_data_from_sheets()
    
    df_papers = pd.DataFrame(papers_data)
    if df_papers.empty: return pd.DataFrame()
    df_papers['doi'] = df_papers['doi'].astype(str)

    df_interest = pd.DataFrame(interest_data)
    if df_interest.empty:
        df_papers['total_votes'] = 0
        df_papers['voter_names'] = ""
        df_papers['my_vote'] = False
        return pd.DataFrame() # Shortlist empty
    
    df_interest['doi'] = df_interest['doi'].astype(str)

    # Stats
    stats = df_interest.groupby('doi').agg(
        total_votes=('user', 'count'),
        voter_names=('user', lambda x: ','.join(x))
    ).reset_index()

    shortlist = pd.merge(df_papers, stats, on='doi', how='inner')
    shortlist = shortlist.sort_values(by=['total_votes', 'date'], ascending=[False, False])
    
    my_voted_dois = df_interest[df_interest['user'] == current_user]['doi'].tolist()
    shortlist['my_vote'] = shortlist['doi'].isin(my_voted_dois)
    
    return shortlist

def get_fresh_stream_by_date(current_user, start_date, end_date):
    # Load from Cache
    papers_data, interest_data, seen_data = load_all_data_from_sheets()
    
    df_p = pd.DataFrame(papers_data)
    if df_p.empty: return pd.DataFrame()
    df_p['doi'] = df_p['doi'].astype(str)
    
    if interest_data:
        voted_dois = set(pd.DataFrame(interest_data)['doi'].astype(str))
    else:
        voted_dois = set()
        
    if seen_data:
        df_seen = pd.DataFrame(seen_data)
        df_seen['doi'] = df_seen['doi'].astype(str)
        my_seen_dois = set(df_seen[df_seen['user'] == current_user]['doi'])
    else:
        my_seen_dois = set()
        
    # Date Filter
    # Robust date conversion
    df_p['date_obj'] = pd.to_datetime(df_p['date']).dt.date
    
    mask_date = (df_p['date_obj'] >= start_date) & (df_p['date_obj'] <= end_date)
    mask_not_voted = ~df_p['doi'].isin(voted_dois)
    mask_not_seen = ~df_p['doi'].isin(my_seen_dois)
    
    fresh_df = df_p[mask_date & mask_not_voted & mask_not_seen].copy()
    fresh_df = fresh_df.sort_values(by='date', ascending=False)
    fresh_df['my_vote'] = False 
    fresh_df['total_votes'] = 0
    
    return fresh_df

# --- BIO-RXIV FETCHING ---
def fetch_papers_range(start_date, end_date):
    s_date = start_date.strftime('%Y-%m-%d')
    e_date = end_date.strftime('%Y-%m-%d')
    
    sh = get_db_connection()
    ws = sh.worksheet("papers")
    existing_data = ws.get_all_records()
    if existing_data:
        seen_dois = set(pd.DataFrame(existing_data)['doi'].astype(str))
    else:
        seen_dois = set()

    cursor = 0
    new_rows = []
    
    progress_text = f"Fetching papers from {s_date} to {e_date}..."
    my_bar = st.sidebar.progress(0, text=progress_text)

    for i in range(5): 
        my_bar.progress((i+1) * 20, text=f"Scanning page {i+1}...")
        url = f"https://api.biorxiv.org/details/biorxiv/{s_date}/{e_date}/{cursor}?category=neuroscience"
        try:
            response = requests.get(url).json()
            if response.get('messages', [{}])[0].get('status') == 'no posts found': break
            papers = response.get('collection', [])
            if not papers: break
            
            for p in papers:
                if p.get('category').lower() == 'neuroscience':
                    if p['doi'] not in seen_dois:
                        seen_dois.add(p['doi'])
                        link = f"https://www.biorxiv.org/content/{p['doi']}v1"
                        row = [p['doi'], p['title'], p['authors'], p['abstract'], link, p['category'], p['date']]
                        new_rows.append(row)
                        
            cursor += len(papers)
            if len(papers) < 100: break
            # Politeness delay to prevent BioRxiv API ban
            time.sleep(0.5) 
        except Exception: break
        
    my_bar.empty()
    
    if new_rows:
        ws.append_rows(new_rows)
        # Clear cache so new papers appear immediately
        st.cache_data.clear()
        return len(new_rows)
    return 0

# --- MAIN APP UI ---
def main():
    st.set_page_config(page_title="LabRxiv", layout="wide") 
    
    st.markdown("""
        <style>
               .block-container { padding-top: 2rem; padding-bottom: 5rem; }
               .badge {
                   display: inline-block; padding: 2px 8px; margin-right: 4px; margin-top: 4px;
                   border-radius: 12px; color: white; font-size: 0.75rem; font-weight: 600; text-transform: uppercase;
               }
               div[data-testid="stButton"] button { margin-top: 0px; }
               
               div[data-testid="stVerticalBlockBorderWrapper"] {
                   border: 1px solid #e0e0e0; transition: all 0.2s ease-in-out;
               }
               div[data-testid="stVerticalBlockBorderWrapper"]:hover {
                   border: 2px solid #ff4b4b !important; 
                   background-color: #f9f9f9;
                   box-shadow: 0 4px 10px rgba(0,0,0,0.1);
                   transform: translateX(2px); 
               }
        </style>
        """, unsafe_allow_html=True)

    # Only run init on first start to check connection
    if 'db_init' not in st.session_state:
        init_db()
        st.session_state['db_init'] = True
    
    st.sidebar.title("🧠 LabRxiv")
    user_name = st.sidebar.selectbox("Current User:", LAB_MEMBERS)
    st.sidebar.divider()
    
    st.sidebar.markdown("**Select Triage Date Range**")
    today = datetime.today()
    last_week = today - timedelta(days=7)
    date_range = st.sidebar.date_input("Range", (last_week, today), format="YYYY-MM-DD")
    
    if isinstance(date_range, tuple) and len(date_range) == 2:
        start_d, end_d = date_range
        if st.sidebar.button(f"⬇️ Load Papers ({start_d} to {end_d})"):
            added = fetch_papers_range(start_d, end_d)
            if added > 0: st.toast(f"Downloaded {added} new papers!")
            else: st.toast("Papers already in database.")
            st.rerun()
    else:
        st.sidebar.warning("Select valid dates.")
        start_d, end_d = today, today

    if user_name == "Select User...":
        st.warning("Please select your name in the sidebar.")
        return

    # --- MAIN CONTENT ---
    all_visible_dois = []
    selected_dois = []

    triaged_df = get_shortlist_data(user_name)
    st.markdown("### 🏆 Lab Shortlist (Active)")
    if triaged_df.empty: st.info("No papers shortlisted yet.")
    
    for index, row in triaged_df.iterrows():
        all_visible_dois.append(row['doi'])
        with st.container(border=True):
            c_check, c_meta, c_content = st.columns([0.03, 0.15, 0.82])
            with c_check:
                if st.checkbox("", value=row['my_vote'], key=f"t_{row['doi']}_{user_name}"):
                    selected_dois.append(row['doi'])
            with c_meta:
                st.markdown(f"### +{row['total_votes']}")
                voters = str(row['voter_names']).split(',') if row['voter_names'] else []
                html_badges = ""
                for v in voters:
                    color = MEMBER_COLORS.get(v, "#7f8c8d")
                    html_badges += f'<span class="badge" style="background-color:{color};">{v}</span>'
                st.markdown(html_badges, unsafe_allow_html=True)
            with c_content:
                with st.expander(f"**{row['title']}**"):
                    st.caption(f"{row['authors']} ({row['date']})")
                    st.write(row['abstract'])
                    st.markdown(f"[Link]({row['link']})")

    st.divider()

    fresh_df = get_fresh_stream_by_date(user_name, start_d, end_d)
    col_fresh_title, col_fresh_count = st.columns([0.8, 0.2])
    col_fresh_title.markdown(f"### 🌊 Fresh Stream ({start_d} to {end_d})")
    if not fresh_df.empty: col_fresh_count.caption(f"Showing {len(fresh_df)} papers")
    
    if fresh_df.empty: st.info(f"No papers found for this range (or you trashed them all).")
    
    for index, row in fresh_df.iterrows():
        all_visible_dois.append(row['doi'])
        with st.container(border=True):
            c_check, c_content, c_trash = st.columns([0.03, 0.92, 0.05])
            with c_check:
                if st.checkbox("", value=row['my_vote'], key=f"f_{row['doi']}_{user_name}"):
                    selected_dois.append(row['doi'])
            with c_content:
                with st.expander(f"{row['title']}"):
                    st.caption(f"{row['authors']} ({row['date']})")
                    st.write(row['abstract'])
                    st.markdown(f"[Link]({row['link']})")
            with c_trash:
                if st.button("🗑️", key=f"trash_{row['doi']}_{user_name}", help="Hide this paper"):
                    mark_as_seen(row['doi'], user_name)
                    st.rerun()

    st.sidebar.divider()
    st.sidebar.markdown("**Done reviewing?**")
    if st.sidebar.button("💾 Submit / Update Votes", type="primary"):
        changes = batch_update_votes(user_name, set(selected_dois), all_visible_dois)
        if changes > 0:
            st.toast(f"Updated {changes} votes!")
            st.rerun()
        else:
            st.toast("No changes detected.")

if __name__ == "__main__":
    main()
