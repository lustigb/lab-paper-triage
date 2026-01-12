import streamlit as st
import requests
import pandas as pd
from supabase import create_client, Client
from datetime import datetime, timedelta
import time

# --- CONFIGURATION ---
LAB_MEMBERS = ["Select User...", "Albert", "Shinsuke", "Jaeson", "Brian"]
TOTAL_LAB_SIZE = len(LAB_MEMBERS) - 1 

MEMBER_COLORS = {
    "Albert": "#3498db",   # Blue
    "Shinsuke": "#2ecc71", # Green
    "Jaeson": "#e67e22",   # Orange
    "Brian": "#e74c3c"     # Red
}

# --- SUPABASE CONNECTION ---
# Initialize connection to Supabase
@st.cache_resource
def init_supabase():
    url = st.secrets["supabase"]["url"]
    key = st.secrets["supabase"]["key"]
    return create_client(url, key)

supabase: Client = init_supabase()

# --- DATA FETCHING (Now optimized with SQL) ---
def load_all_data():
    # 1. Fetch Papers (We limit to recent for speed if needed, but 1000 is fine for now)
    # Note: Supabase limits rows to 1000 by default. 
    # For a real app, we would paginate, but for a prototype this is fine.
    response_papers = supabase.table("papers").select("*").execute()
    papers_data = response_papers.data

    response_interest = supabase.table("interest").select("*").execute()
    interest_data = response_interest.data

    response_seen = supabase.table("seen").select("*").execute()
    seen_data = response_seen.data

    return papers_data, interest_data, seen_data

# --- BATCH UPDATE (The "Smart" SQL Update) ---
def batch_update_all(user, selected_dois, trashed_dois, all_displayed_dois):
    # This logic is smarter than Google Sheets. 
    # We calculate the "Delta" (What to add, What to remove) 
    # instead of wiping the whole sheet.
    
    # 1. Get current votes for this user
    response = supabase.table("interest").select("doi").eq("user", user).execute()
    current_votes = {row['doi'] for row in response.data}
    
    selected_set = set(selected_dois)
    
    # Calculate Deltas
    to_add = selected_set - current_votes
    to_remove = current_votes - selected_set
    
    # Only remove if it was visible in the current list 
    # (Prevent accidental removal of papers not currently on screen)
    visible_set = set(all_displayed_dois)
    to_remove = to_remove.intersection(visible_set)

    changes_count = 0
    
    # EXECUTE SQL INSERTS (Votes)
    if to_add:
        data_to_insert = [{"doi": doi, "user": user, "timestamp": datetime.now().isoformat()} for doi in to_add]
        supabase.table("interest").insert(data_to_insert).execute()
        changes_count += len(to_add)
        
    # EXECUTE SQL DELETES (Unvotes)
    if to_remove:
        # Delete where user=user AND doi is in to_remove list
        supabase.table("interest").delete().eq("user", user).in_("doi", list(to_remove)).execute()
        changes_count += len(to_remove)
        
    # EXECUTE SQL INSERTS (Trash)
    if trashed_dois:
        # Check what is already trashed to avoid duplicates (optional but good)
        trash_list = list(trashed_dois)
        trash_data = [{"doi": doi, "user": user} for doi in trash_list]
        supabase.table("seen").insert(trash_data).execute()
        changes_count += len(trash_list)

    return changes_count

# --- DATA PROCESSING ---
def get_shortlist_data(current_user):
    papers_data, interest_data, _ = load_all_data()
    
    df_papers = pd.DataFrame(papers_data)
    if df_papers.empty: return pd.DataFrame()
    
    df_interest = pd.DataFrame(interest_data)
    if df_interest.empty:
        df_papers['total_votes'] = 0
        df_papers['voter_names'] = ""
        df_papers['my_vote'] = False
        return pd.DataFrame()
    
    # Group By DOI to get counts and names
    stats = df_interest.groupby('doi').agg(
        total_votes=('user', 'count'),
        voter_names=('user', lambda x: ','.join(x))
    ).reset_index()

    shortlist = pd.merge(df_papers, stats, on='doi', how='inner')
    shortlist = shortlist.sort_values(by=['total_votes', 'date'], ascending=[False, False])
    
    # Frozen Order
    if 'shortlist_order' in st.session_state:
        frozen_order = st.session_state['shortlist_order']
        shortlist['doi_cat'] = pd.Categorical(shortlist['doi'], categories=frozen_order, ordered=True)
        shortlist = shortlist.sort_values('doi_cat')
    else:
        st.session_state['shortlist_order'] = shortlist['doi'].tolist()

    my_voted_dois = df_interest[df_interest['user'] == current_user]['doi'].tolist()
    shortlist['my_vote'] = shortlist['doi'].isin(my_voted_dois)
    
    return shortlist

def get_fresh_stream_by_date(current_user, start_date, end_date):
    papers_data, interest_data, seen_data = load_all_data()
    
    df_p = pd.DataFrame(papers_data)
    if df_p.empty: return pd.DataFrame()
    
    if interest_data:
        voted_dois = set(pd.DataFrame(interest_data)['doi'])
    else:
        voted_dois = set()
        
    if seen_data:
        df_seen = pd.DataFrame(seen_data)
        my_seen_dois = set(df_seen[df_seen['user'] == current_user]['doi'])
    else:
        my_seen_dois = set()
        
    # Date Filtering
    df_p['date_obj'] = pd.to_datetime(df_p['date']).dt.date
    mask_date = (df_p['date_obj'] >= start_date) & (df_p['date_obj'] <= end_date)
    mask_not_voted = ~df_p['doi'].isin(voted_dois)
    mask_not_seen = ~df_p['doi'].isin(my_seen_dois)
    
    fresh_df = df_p[mask_date & mask_not_voted & mask_not_seen].copy()
    fresh_df = fresh_df.sort_values(by='date', ascending=False)
    fresh_df['my_vote'] = False 
    fresh_df['total_votes'] = 0
    
    return fresh_df

def fetch_papers_range(start_date, end_date):
    s_date = start_date.strftime('%Y-%m-%d')
    e_date = end_date.strftime('%Y-%m-%d')
    
    # 1. Check what we already have to avoid duplicate fetching
    existing_response = supabase.table("papers").select("doi").execute()
    existing_dois = {row['doi'] for row in existing_response.data}
    
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
                    if p['doi'] not in existing_dois:
                        existing_dois.add(p['doi'])
                        link = f"https://www.biorxiv.org/content/{p['doi']}v1"
                        row = {
                            "doi": p['doi'],
                            "title": p['title'],
                            "authors": p['authors'],
                            "abstract": p['abstract'],
                            "link": link,
                            "category": p['category'],
                            "date": p['date']
                        }
                        new_rows.append(row)
                        
            cursor += len(papers)
            if len(papers) < 100: break
            time.sleep(0.5) 
        except Exception: break
        
    my_bar.empty()
    
    if new_rows:
        # Bulk Insert into Supabase
        supabase.table("papers").insert(new_rows).execute()
        return len(new_rows)
    return 0

# --- MAIN APP UI (Identical to V8.1) ---
def main():
    st.set_page_config(page_title="LabRxiv", layout="wide") 
    
    st.markdown("""
        <style>
               .block-container { padding-top: 2rem; padding-bottom: 5rem; }
               .badge {
                   display: inline-block; padding: 2px 8px; margin-right: 4px; margin-top: 4px;
                   border-radius: 12px; color: white; font-size: 0.75rem; font-weight: 600; text-transform: uppercase;
               }
               div[data-testid="stVerticalBlockBorderWrapper"] {
                   border: 1px solid #e0e0e0; transition: all 0.2s ease-in-out;
               }
               div[data-testid="stVerticalBlockBorderWrapper"]:hover {
                   border: 2px solid #ff4b4b !important; 
                   background-color: #f9f9f9;
                   box-shadow: 0 4px 10px rgba(0,0,0,0.1);
                   transform: translateX(2px); 
               }
               .share-text { font-size: 1.1rem; font-weight: 800; color: #444; line-height: 1.2; margin-bottom: 4px; }
               div[data-testid="stButton"] { text-align: center; }
        </style>
        """, unsafe_allow_html=True)
    
    st.sidebar.title("🧠 LabRxiv")
    user_name = st.sidebar.selectbox("Current User:", LAB_MEMBERS)
    st.sidebar.divider()
    
    st.sidebar.markdown("**View Settings**")
    expand_all = st.sidebar.toggle("👁️ Abstract View", value=False)
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
        start_d, end_d = today, today

    if user_name == "Select User...":
        st.warning("Please select your name in the sidebar.")
        return

    all_visible_dois = []
    selected_dois = []
    trashed_dois = []

    triaged_df = get_shortlist_data(user_name)
    total_system_votes = triaged_df['total_votes'].sum() if not triaged_df.empty else 1

    st.markdown("### 🏆 Lab Shortlist (Active)")
    if triaged_df.empty: st.info("No papers shortlisted yet.")
    
    for index, row in triaged_df.iterrows():
        doi = row['doi']
        all_visible_dois.append(doi)
        
        db_voted = row['my_vote']
        toggle_key = f"vote_state_{doi}_{user_name}"
        if toggle_key not in st.session_state: st.session_state[toggle_key] = False
        user_clicked_toggle = st.session_state[toggle_key]
        
        is_effectively_selected = (db_voted != user_clicked_toggle)
        if is_effectively_selected: selected_dois.append(doi)
        
        if db_voted and not user_clicked_toggle: btn_label = "🗑️"
        elif db_voted and user_clicked_toggle: btn_label = "❌ Remove"
        elif not db_voted and user_clicked_toggle: btn_label = "✅ Voted"
        else: btn_label = "👍"

        with st.container(border=True):
            c_vote, c_btn, c_content = st.columns([0.12, 0.12, 0.76])
            with c_vote:
                share_pct = row['total_votes'] / total_system_votes
                st.markdown(f'<div class="share-text">{share_pct:.0%} Share</div>', unsafe_allow_html=True)
                st.progress(share_pct)
            with c_btn:
                voters = str(row['voter_names']).split(',') if row['voter_names'] else []
                if voters:
                    with st.expander(f"👥 {len(voters)}"):
                        html_badges = ""
                        for v in voters:
                            color = MEMBER_COLORS.get(v, "#7f8c8d")
                            html_badges += f'<span class="badge" style="background-color:{color};">{v}</span>'
                        st.markdown(html_badges, unsafe_allow_html=True)
                if st.button(btn_label, type="secondary", key=f"btn_{doi}_{user_name}"):
                    st.session_state[toggle_key] = not st.session_state[toggle_key]
                    st.rerun()
            with c_content:
                with st.expander(f"**{row['title']}**", expanded=expand_all):
                    st.caption(f"{row['authors']} ({row['date']})")
                    st.write(row['abstract'])
                    st.markdown(f"[Link]({row['link']})")

    st.divider()

    fresh_df = get_fresh_stream_by_date(user_name, start_d, end_d)
    c_fresh_h, c_fresh_cnt = st.columns([0.8, 0.2])
    c_fresh_h.markdown(f"### 🌊 Fresh Stream ({start_d} to {end_d})")
    if not fresh_df.empty: c_fresh_cnt.caption(f"Showing {len(fresh_df)} papers")
    
    if fresh_df.empty: st.info(f"No papers found for this range.")
    
    for index, row in fresh_df.iterrows():
        doi = row['doi']
        all_visible_dois.append(doi)
        
        vote_key = f"vote_state_{doi}_{user_name}"
        if vote_key not in st.session_state: st.session_state[vote_key] = False
        user_clicked_vote = st.session_state[vote_key]
        if user_clicked_vote:
            selected_dois.append(doi)
            vote_label = "✅ Voted"
        else:
            vote_label = "👍"

        trash_key = f"trash_state_{doi}_{user_name}"
        if trash_key not in st.session_state: st.session_state[trash_key] = False
        user_clicked_trash = st.session_state[trash_key]
        if user_clicked_trash:
            trashed_dois.append(doi)
            trash_label = "❌ Remove"
        else:
            trash_label = "🗑️"

        with st.container(border=True):
            c_vote_btn, c_trash_btn, c_content = st.columns([0.10, 0.10, 0.80])
            with c_vote_btn:
                if st.button(vote_label, type="secondary", key=f"f_v_btn_{doi}_{user_name}"):
                    st.session_state[vote_key] = not st.session_state[vote_key]
                    st.rerun()
            with c_trash_btn:
                if st.button(trash_label, type="secondary", key=f"f_t_btn_{doi}_{user_name}"):
                    st.session_state[trash_key] = not st.session_state[trash_key]
                    st.rerun()
            with c_content:
                with st.expander(f"{row['title']}", expanded=expand_all):
                    st.caption(f"{row['authors']} ({row['date']})")
                    st.write(row['abstract'])
                    st.markdown(f"[Link]({row['link']})")

    st.sidebar.divider()
    if st.sidebar.button("💾 Submit Votes", type="primary"):
        changes = batch_update_all(user_name, set(selected_dois), set(trashed_dois), all_visible_dois)
        keys_to_reset = [k for k in st.session_state.keys() if (f"vote_state_" in k or f"trash_state_" in k) and user_name in k]
        for k in keys_to_reset: st.session_state[k] = False
            
        if changes > 0:
            st.toast(f"Processed {changes} updates!")
            if 'shortlist_order' in st.session_state: del st.session_state['shortlist_order']
            st.rerun()
        else:
            st.toast("No changes detected.")

if __name__ == "__main__":
    main()
