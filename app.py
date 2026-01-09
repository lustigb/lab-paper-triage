import streamlit as st
import requests
import pandas as pd
import gspread
from datetime import datetime, timedelta

# --- CONFIGURATION ---
LAB_MEMBERS = ["Select User...", "Albert", "Shinsuke", "Jaeson", "Brian"]

MEMBER_COLORS = {
    "Albert": "#3498db",   # Blue
    "Shinsuke": "#2ecc71", # Green
    "Jaeson": "#e67e22",   # Orange
    "Brian": "#e74c3c"     # Red
}

# --- GOOGLE SHEETS CONNECTION ---
def get_db_connection():
    """Establishes connection to Google Sheets using Streamlit Secrets."""
    try:
        # Create a gspread client using the secrets
        gc = gspread.service_account_from_dict(st.secrets["gcp_service_account"])
        # Open the sheet by URL
        sh = gc.open_by_url(st.secrets["private_gsheets_url"])
        return sh
    except Exception as e:
        st.error(f"Failed to connect to Google Sheets: {e}")
        st.stop()

def init_db():
    """Checks if necessary worksheets exist, creates headers if missing."""
    sh = get_db_connection()
    
    # 1. PAPERS SHEET
    try:
        ws_papers = sh.worksheet("papers")
    except:
        ws_papers = sh.add_worksheet(title="papers", rows=1000, cols=7)
        ws_papers.append_row(["doi", "title", "authors", "abstract", "link", "category", "date"])

    # 2. INTEREST SHEET (Votes)
    try:
        ws_interest = sh.worksheet("interest")
    except:
        ws_interest = sh.add_worksheet(title="interest", rows=1000, cols=3)
        ws_interest.append_row(["doi", "user", "timestamp"])

    # 3. SEEN SHEET (Trash)
    try:
        ws_seen = sh.worksheet("seen")
    except:
        ws_seen = sh.add_worksheet(title="seen", rows=1000, cols=2)
        ws_seen.append_row(["doi", "user"])

def mark_as_seen(doi, user):
    """Appends a row to the 'seen' worksheet."""
    sh = get_db_connection()
    ws = sh.worksheet("seen")
    # Check if already exists to avoid duplicates (Client-side check expensive, just append unique?)
    # For speed in GSheets, we often just append. We can dedup in Pandas on read.
    # But let's do a quick check to keep sheet clean.
    
    # Actually, simpler: Just Append. We filter duplicates on Read.
    ws.append_row([doi, user])

def batch_update_votes(user, selected_dois, all_displayed_dois):
    """
    Syncs votes to Google Sheets.
    Strategy: Read current votes, calculate delta, update sheet.
    This is 'Nuclear' but safe: Read All -> Logic -> Write Back is dangerous with concurrency.
    Better: Append new votes. To remove votes, we must find and delete.
    """
    sh = get_db_connection()
    ws = sh.worksheet("interest")
    
    # 1. Fetch current interest data
    data = ws.get_all_records()
    df = pd.DataFrame(data)
    
    # Ensure columns exist (handle empty sheet case)
    if df.empty:
        df = pd.DataFrame(columns=["doi", "user", "timestamp"])
    else:
        # Convert all to string to ensure matching
        df['doi'] = df['doi'].astype(str)
        df['user'] = df['user'].astype(str)

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    changes_count = 0
    
    # We need to rebuild the interest list for THIS user based on displayed DOIs
    
    # List of DOIs this user currently has in the DB
    current_user_votes = df[df['user'] == user]['doi'].tolist()
    
    rows_to_add = []
    # We can't easily batch delete scattered rows in GSheets.
    # Strategy: We will identify rows to DELETE and rows to ADD.
    # To delete, we will clear the sheet and rewrite the updated DF. 
    # (Note: This is risky if 2 people vote EXACTLY at the same time, but acceptable for a small lab).
    
    modified = False
    
    for doi in all_displayed_dois:
        is_checked = doi in selected_dois
        already_voted = doi in current_user_votes
        
        if is_checked and not already_voted:
            # Add new vote
            new_row = {"doi": doi, "user": user, "timestamp": timestamp}
            rows_to_add.append(new_row)
            modified = True
            changes_count += 1
            
        elif not is_checked and already_voted:
            # Remove vote
            # Filter the DF to exclude this specific row
            df = df[~((df['doi'] == doi) & (df['user'] == user))]
            modified = True
            changes_count += 1

    if modified:
        # If we added rows, concat them to DF
        if rows_to_add:
            df_add = pd.DataFrame(rows_to_add)
            df = pd.concat([df, df_add], ignore_index=True)
        
        # WRITE BACK STRATEGY
        # 1. Clear Sheet
        ws.clear()
        # 2. Add Header
        ws.append_row(["doi", "user", "timestamp"])
        # 3. Add Data
        # gspread expects a list of lists
        if not df.empty:
            ws.append_rows(df.values.tolist())
            
    return changes_count

def get_shortlist_data(current_user):
    sh = get_db_connection()
    
    # Fetch Papers
    papers_data = sh.worksheet("papers").get_all_records()
    df_papers = pd.DataFrame(papers_data)
    if df_papers.empty: return pd.DataFrame()
    df_papers['doi'] = df_papers['doi'].astype(str)

    # Fetch Interests
    interest_data = sh.worksheet("interest").get_all_records()
    df_interest = pd.DataFrame(interest_data)
    
    if df_interest.empty:
        df_papers['total_votes'] = 0
        df_papers['voter_names'] = ""
        df_papers['my_vote'] = False
        return pd.DataFrame() # Return empty if no votes at all? No, technically shortlist is empty.
    
    df_interest['doi'] = df_interest['doi'].astype(str)

    # Aggregate Votes
    # Group by DOI, count votes, and join user names
    stats = df_interest.groupby('doi').agg(
        total_votes=('user', 'count'),
        voter_names=('user', lambda x: ','.join(x))
    ).reset_index()

    # Merge with Papers
    # Inner join because shortlist only shows voted papers
    shortlist = pd.merge(df_papers, stats, on='doi', how='inner')
    
    # Sort
    shortlist = shortlist.sort_values(by=['total_votes', 'date'], ascending=[False, False])
    
    # Determine 'my_vote'
    my_voted_dois = df_interest[df_interest['user'] == current_user]['doi'].tolist()
    shortlist['my_vote'] = shortlist['doi'].isin(my_voted_dois)
    
    return shortlist

def get_fresh_stream_by_date(current_user, start_date, end_date):
    sh = get_db_connection()
    
    # 1. Fetch Papers
    papers_data = sh.worksheet("papers").get_all_records()
    df_p = pd.DataFrame(papers_data)
    if df_p.empty: return pd.DataFrame()
    df_p['doi'] = df_p['doi'].astype(str)
    
    # 2. Fetch Interests (to exclude voted)
    interest_data = sh.worksheet("interest").get_all_records()
    if interest_data:
        voted_dois = set(pd.DataFrame(interest_data)['doi'].astype(str))
    else:
        voted_dois = set()
        
    # 3. Fetch Seen (to exclude trashed)
    seen_data = sh.worksheet("seen").get_all_records()
    if seen_data:
        df_seen = pd.DataFrame(seen_data)
        df_seen['doi'] = df_seen['doi'].astype(str)
        my_seen_dois = set(df_seen[df_seen['user'] == current_user]['doi'])
    else:
        my_seen_dois = set()
        
    # 4. Filter
    # Filter by Date
    df_p['date'] = pd.to_datetime(df_p['date']).dt.date
    # Convert inputs to date objects if they are strings (streamlit date_input returns date objects usually)
    
    mask_date = (df_p['date'] >= start_date) & (df_p['date'] <= end_date)
    mask_not_voted = ~df_p['doi'].isin(voted_dois)
    mask_not_seen = ~df_p['doi'].isin(my_seen_dois)
    
    fresh_df = df_p[mask_date & mask_not_voted & mask_not_seen].copy()
    
    fresh_df = fresh_df.sort_values(by='date', ascending=False)
    fresh_df['my_vote'] = False # Fresh papers are by definition not voted by anyone
    fresh_df['total_votes'] = 0
    
    return fresh_df

# --- BIO-RXIV FETCHING ---
def fetch_papers_range(start_date, end_date):
    s_date = start_date.strftime('%Y-%m-%d')
    e_date = end_date.strftime('%Y-%m-%d')
    
    # Get existing DOIs from Sheet to avoid duplicates
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
                        
                        # Prepare Row
                        row = [
                            p['doi'], 
                            p['title'], 
                            p['authors'], 
                            p['abstract'], 
                            link, 
                            p['category'], 
                            p['date']
                        ]
                        new_rows.append(row)
                        
            cursor += len(papers)
            if len(papers) < 100: break
        except Exception: break
        
    my_bar.empty()
    
    # Bulk Append to Sheet
    if new_rows:
        ws.append_rows(new_rows)
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
               
               /* --- SIMPLE HOVER --- */
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

    # Initialize Sheet Tabs
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

    # 1. SHORTLIST
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

    # 2. FRESH STREAM
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

    # --- SUBMIT ---
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
