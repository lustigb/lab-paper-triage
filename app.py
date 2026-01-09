import streamlit as st
import requests
import sqlite3
import pandas as pd
from datetime import datetime, timedelta

# --- CONFIGURATION ---
DB_FILE = "labrxiv.db"
# Update this list with your actual lab members
LAB_MEMBERS = ["Select User...", "Albert", "Shinsuke", "Jaeson", "Brian"]

# Colors for the User Badges
MEMBER_COLORS = {
    "Albert": "#3498db",   # Blue
    "Shinsuke": "#2ecc71", # Green
    "Jaeson": "#e67e22",   # Orange
    "Brian": "#e74c3c"     # Red
}

# --- DATABASE FUNCTIONS ---
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    # 1. Papers content
    c.execute('''CREATE TABLE IF NOT EXISTS papers
                 (doi TEXT PRIMARY KEY, title TEXT, authors TEXT, 
                  abstract TEXT, link TEXT, category TEXT, date TEXT)''')
    # 2. Votes/Interest
    c.execute('''CREATE TABLE IF NOT EXISTS interest
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                  doi TEXT, user TEXT, timestamp DATETIME)''')
    # 3. Seen/Trash tracking
    c.execute('''CREATE TABLE IF NOT EXISTS seen
                 (doi TEXT, user TEXT, PRIMARY KEY (doi, user))''')
    conn.commit()
    conn.close()

def mark_as_seen(doi, user):
    """Marks a paper as 'trashed' for a specific user so it hides from their feed."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO seen (doi, user) VALUES (?, ?)", (doi, user))
    conn.commit()
    conn.close()

def batch_update_votes(user, selected_dois, all_displayed_dois):
    """Syncs the checkbox state with the database."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    timestamp = datetime.now()
    changes = 0
    
    for doi in all_displayed_dois:
        c.execute("SELECT * FROM interest WHERE doi=? AND user=?", (doi, user))
        already_voted = c.fetchone() is not None
        is_checked = doi in selected_dois
        
        # User checked it -> Add vote
        if is_checked and not already_voted:
            c.execute("INSERT INTO interest (doi, user, timestamp) VALUES (?, ?, ?)", 
                      (doi, user, timestamp))
            changes += 1
        # User unchecked it -> Remove vote
        elif not is_checked and already_voted:
            c.execute("DELETE FROM interest WHERE doi=? AND user=?", (doi, user))
            changes += 1
            
    conn.commit()
    conn.close()
    return changes

def get_shortlist_data(current_user):
    """Fetches papers with >0 votes, plus the names of who voted."""
    conn = sqlite3.connect(DB_FILE)
    query = """
    SELECT p.title, p.authors, p.abstract, p.link, p.doi, p.date, 
           COUNT(i.doi) as total_votes,
           GROUP_CONCAT(i.user, ',') as voter_names
    FROM papers p
    JOIN interest i ON p.doi = i.doi
    GROUP BY p.doi
    ORDER BY total_votes DESC, p.date DESC
    """
    df = pd.read_sql_query(query, conn)
    
    # Add 'my_vote' column for the checkbox state
    if not df.empty:
        user_votes = pd.read_sql_query("SELECT doi FROM interest WHERE user=?", conn, params=(current_user,))
        user_vote_set = set(user_votes['doi'])
        df['my_vote'] = df['doi'].apply(lambda x: x in user_vote_set)
    else:
        df['my_vote'] = False
    conn.close()
    return df

def get_fresh_stream_by_date(current_user, start_date, end_date):
    """Fetches 0-vote papers in date range that user hasn't trashed."""
    conn = sqlite3.connect(DB_FILE)
    query = """
    SELECT p.*, 0 as total_votes
    FROM papers p
    WHERE p.doi NOT IN (SELECT distinct doi FROM interest)
      AND p.doi NOT IN (SELECT doi FROM seen WHERE user=?) 
      AND p.date BETWEEN ? AND ?
    ORDER BY p.date DESC
    """
    df = pd.read_sql_query(query, conn, params=(current_user, str(start_date), str(end_date)))
    df['my_vote'] = False
    conn.close()
    return df

# --- BIO-RXIV FETCHING ---
def fetch_papers_range(start_date, end_date):
    s_date = start_date.strftime('%Y-%m-%d')
    e_date = end_date.strftime('%Y-%m-%d')
    seen_dois = set()
    cursor = 0
    count_added = 0
    
    progress_text = f"Fetching papers from {s_date} to {e_date}..."
    my_bar = st.sidebar.progress(0, text=progress_text)

    # Fetch up to 5 pages (500 papers) to be safe
    for i in range(5): 
        my_bar.progress((i+1) * 20, text=f"Scanning page {i+1}...")
        url = f"https://api.biorxiv.org/details/biorxiv/{s_date}/{e_date}/{cursor}?category=neuroscience"
        try:
            response = requests.get(url).json()
            if response.get('messages', [{}])[0].get('status') == 'no posts found': break
            papers = response.get('collection', [])
            if not papers: break
            
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            for p in papers:
                if p.get('category').lower() == 'neuroscience':
                    if p['doi'] not in seen_dois:
                        seen_dois.add(p['doi'])
                        link = f"https://www.biorxiv.org/content/{p['doi']}v1"
                        try:
                            c.execute('''INSERT INTO papers 
                                         (doi, title, authors, abstract, link, category, date) 
                                         VALUES (?, ?, ?, ?, ?, ?, ?)''', 
                                         (p['doi'], p['title'], p['authors'], p['abstract'], 
                                          link, p['category'], p['date']))
                            count_added += 1
                        except sqlite3.IntegrityError:
                            pass 
            conn.commit()
            conn.close()
            cursor += len(papers)
            if len(papers) < 100: break
        except Exception: break
        
    my_bar.empty()
    return count_added

# --- MAIN APP UI ---
def main():
    st.set_page_config(page_title="LabRxiv", layout="wide") 
    
    # --- CLEAN CSS (ESSENTIALS ONLY) ---
    st.markdown("""
        <style>
               /* Fix top padding */
               .block-container {
                    padding-top: 2rem;
                    padding-bottom: 5rem;
               }
               /* User Badges Styling */
               .badge {
                   display: inline-block;
                   padding: 2px 8px;
                   margin-right: 4px;
                   margin-top: 4px;
                   border-radius: 12px;
                   color: white;
                   font-size: 0.75rem;
                   font-weight: 600;
                   text-transform: uppercase;
               }
               /* Align trash button */
               div[data-testid="stButton"] button {
                   margin-top: 0px; 
               }
        </style>
        """, unsafe_allow_html=True)

    init_db()
    
    # --- SIDEBAR ---
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

    # 1. SHORTLIST (Active Papers)
    triaged_df = get_shortlist_data(user_name)
    st.markdown("### 🏆 Lab Shortlist (Active)")
    if triaged_df.empty: st.info("No papers shortlisted yet.")
    
    for index, row in triaged_df.iterrows():
        all_visible_dois.append(row['doi'])
        
        with st.container(border=True):
            # Column Ratios: Checkbox | Metadata | Content
            c_check, c_meta, c_content = st.columns([0.03, 0.15, 0.82])
            
            with c_check:
                # Key includes user_name to prevent state persistence issues
                if st.checkbox("", value=row['my_vote'], key=f"t_{row['doi']}_{user_name}"):
                    selected_dois.append(row['doi'])
            
            with c_meta:
                st.markdown(f"### +{row['total_votes']}")
                voters = row['voter_names'].split(',') if row['voter_names'] else []
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

    # 2. FRESH STREAM (Filtered by Date & Trash)
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

    # --- SUBMIT BUTTON ---
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