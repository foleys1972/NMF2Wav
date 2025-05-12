import tkinter as tk
from tkinter import messagebox, ttk, simpledialog
import json
import threading
import time
import os
import socket
from collections import deque
from websocket import create_connection, WebSocketConnectionClosedException
import queue
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

class TradeSenseDialer:
    def __init__(self, root):
        self.root = root
        self.root.title("TradeSense Dialer Pro v2.0")
        
        # Configuration
        self.ws = None
        self.connected = False
        self.auth_timeout = 1800  # Default session timeout (30 mins)
        self.last_auth_time = 0
        self.calling = False
        self.config_file = "tradesense_config.json"
        self.call_status = {}
        self.recent_calls = deque(maxlen=50)  # Increased history size

        self.pending_responses = {}  # For async command/response matching
        self.listen_thread_running = False

        self.dial_stats = {"success": 0, "failed": 0}
        self.quick_numbers = ["100", "911", "999"]
        self.sites = {}  # Dictionary to store multiple sites
        self.current_site = None
        
        # User presence tracking
        self.user_presence = {}  # Dictionary to track user presence status
        
        # UI Setup
        self.setup_ui()
        self.load_config()
        
    def setup_ui(self):
        """Setup the main application UI"""
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True)
        
        # Main Tab
        self.main_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.main_frame, text="Dialer")
        
        # Status Tab
        self.status_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.status_frame, text="Call Status")
        
        # Site Management Frame
        site_frame = ttk.LabelFrame(self.main_frame, text="Site Management", padding=10)
        site_frame.pack(fill=tk.X, pady=5)
        
        # Site selection combobox
        self.site_combo = ttk.Combobox(site_frame, state="readonly", width=30)
        self.site_combo.grid(row=0, column=0, padx=5)
        self.site_combo.bind("<<ComboboxSelected>>", self.on_site_selected)
        
        ttk.Button(site_frame, text="New Site", command=self.add_site).grid(row=0, column=1, padx=2)
        ttk.Button(site_frame, text="Save Site", command=self.save_current_site).grid(row=0, column=2, padx=2)
        ttk.Button(site_frame, text="Delete Site", command=self.delete_site).grid(row=0, column=3, padx=2)
        
        # Connection Frame
        conn_frame = ttk.LabelFrame(self.main_frame, text="Connection", padding=10)
        conn_frame.pack(fill=tk.X, pady=5)
        
        ttk.Label(conn_frame, text="WebSocket URL:").grid(row=0, column=0, sticky='e')
        self.ws_url = ttk.Entry(conn_frame, width=40)
        self.ws_url.grid(row=0, column=1, padx=5)
        
        ttk.Label(conn_frame, text="Token:").grid(row=1, column=0, sticky='e')
        self.token = ttk.Entry(conn_frame, width=40)
        self.token.grid(row=1, column=1, padx=5)
        
        ttk.Button(conn_frame, text="Connect", command=self.connect).grid(row=0, column=2, padx=5)
        ttk.Button(conn_frame, text="Disconnect", command=self.disconnect).grid(row=1, column=2, padx=5)
        self.conn_status = ttk.Label(conn_frame, text="Disconnected", foreground="red")
        self.conn_status.grid(row=0, column=3, rowspan=2, padx=5)
        
        # Quick Dial Frame
        quick_frame = ttk.LabelFrame(self.main_frame, text="Quick Dial", padding=10)
        quick_frame.pack(fill=tk.X, pady=5)
        
        self.quick_buttons = []
        for i, num in enumerate(self.quick_numbers):
            btn = ttk.Button(quick_frame, text=num, width=5,
                            command=lambda n=num: self.quick_dial(n))
            btn.grid(row=0, column=i, padx=2)
            self.quick_buttons.append(btn)
            
        ttk.Button(quick_frame, text="+", width=3,
                  command=self.add_quick_number).grid(row=0, column=len(self.quick_numbers), padx=5)
        
        # User Frame
        user_frame = ttk.LabelFrame(self.main_frame, text="Users", padding=10)
        user_frame.pack(fill=tk.BOTH, expand=True, pady=5)
        
        # Search Frame
        search_frame = ttk.Frame(user_frame)
        search_frame.pack(fill=tk.X, pady=(0, 5))
        
        ttk.Label(search_frame, text="Search:").pack(side=tk.LEFT, padx=(0, 5))
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(search_frame, textvariable=self.search_var)
        self.search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))
        
        # Search operator combobox
        self.search_operator = ttk.Combobox(search_frame, width=12, state="readonly")
        self.search_operator['values'] = ('contains', 'is_exactly', 'begins_with')
        self.search_operator.set('contains')
        self.search_operator.pack(side=tk.LEFT, padx=(0, 5))
        
        ttk.Button(search_frame, text="Search", command=self.search_users).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(search_frame, text="Clear", command=self.clear_search).pack(side=tk.LEFT)
        
        # Filter Frame
        filter_frame = ttk.Frame(user_frame)
        filter_frame.pack(fill=tk.X, pady=(0, 5))
        
        # Filter by status checkbox
        self.show_all_users = tk.BooleanVar(value=True)
        self.show_offline_users = tk.BooleanVar(value=False)
        ttk.Checkbutton(filter_frame, text="Show All Users", variable=self.show_all_users, 
                      command=self.apply_user_filters).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Checkbutton(filter_frame, text="Show Offline Users", variable=self.show_offline_users, 
                      command=self.apply_user_filters).pack(side=tk.LEFT, padx=(0, 5))
        
        # User TreeView with presence status indicators
        self.user_tree = ttk.Treeview(user_frame, columns=("login", "name", "turret", "status"), show="headings")
        self.user_tree.heading("login", text="Login")
        self.user_tree.heading("name", text="Name")
        self.user_tree.heading("turret", text="Turret")
        self.user_tree.heading("status", text="Status")
        
        # Configure status tags for visual indicators 
        self.user_tree.tag_configure('online', foreground='green')
        self.user_tree.tag_configure('offline', foreground='gray')
        self.user_tree.tag_configure('busy', foreground='red')
        
        self.user_tree.pack(fill=tk.BOTH, expand=True)
        
        # Add scrollbar to user tree
        user_scrollbar = ttk.Scrollbar(user_frame, orient="vertical", command=self.user_tree.yview)
        self.user_tree.configure(yscrollcommand=user_scrollbar.set)
        user_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        # Buttons frame
        button_frame = ttk.Frame(user_frame)
        button_frame.pack(fill=tk.X, pady=5)
        
        ttk.Button(button_frame, text="Refresh All Users", command=self.fetch_users).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(button_frame, text="Get Presence Status", command=self.get_presence_status).pack(side=tk.LEFT, padx=(0, 5))
        
        self.user_count_label = ttk.Label(button_frame, text="Users: 0 (0 online)")
        self.user_count_label.pack(side=tk.RIGHT, padx=(5, 0))
        
        # Dial Frame
        dial_frame = ttk.LabelFrame(self.main_frame, text="Dial Settings", padding=10)
        dial_frame.pack(fill=tk.X, pady=5)
        
        ttk.Label(dial_frame, text="Number:").grid(row=0, column=0, sticky='e')
        self.number = ttk.Entry(dial_frame)
        self.number.grid(row=0, column=1, padx=5, sticky='we')
        
        ttk.Label(dial_frame, text="Interval (sec):").grid(row=0, column=2, sticky='e')
        self.interval = ttk.Spinbox(dial_frame, from_=1, to=60, width=5)
        self.interval.set(5)
        self.interval.grid(row=0, column=3, padx=5)
        
        # Multiple user dialing option
        self.multi_dial_var = tk.BooleanVar()
        ttk.Checkbutton(dial_frame, text="Multi-User", variable=self.multi_dial_var).grid(row=0, column=4, padx=5)
        
        # Device selection (optional)
        ttk.Label(dial_frame, text="Device:").grid(row=1, column=0, sticky='e')
        self.device_combo = ttk.Combobox(dial_frame, width=10, state="readonly")
        self.device_combo['values'] = ('', 'HS1', 'HS2', 'LD1.1', 'LD1.2', 'LD2.1', 'LD2.2')
        self.device_combo.set('')
        self.device_combo.grid(row=1, column=1, sticky='w', padx=5)
        
        # Concurrent calls limit for multi-user mode
        ttk.Label(dial_frame, text="Max Concurrent:").grid(row=1, column=2, sticky='e')
        self.max_concurrent = ttk.Spinbox(dial_frame, from_=1, to=10, width=5)
        self.max_concurrent.set(3)
        self.max_concurrent.grid(row=1, column=3, padx=5)
        
        # Skip offline users option
        self.skip_offline_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(dial_frame, text="Skip Offline Users", variable=self.skip_offline_var).grid(row=1, column=4, padx=5)
        
        self.start_button = ttk.Button(dial_frame, text="Start Dialing", command=self.start_dialing)
        self.start_button.grid(row=0, column=5, padx=5)
        
        ttk.Button(dial_frame, text="Stop Dialing", command=self.stop_dialing).grid(row=1, column=5, padx=5)
        
        # Stats Label
        self.stats_label = ttk.Label(self.main_frame, 
                                    text="Success: 0 | Failed: 0")
        self.stats_label.pack(anchor='e', padx=10)
        
        # Status Tab Content
        self.status_tree = ttk.Treeview(self.status_frame, columns=("time", "user", "number", "status"), show="headings")
        self.status_tree.heading("time", text="Time")
        self.status_tree.heading("user", text="User")
        self.status_tree.heading("number", text="Number")
        self.status_tree.heading("status", text="Status")
        self.status_tree.pack(fill=tk.BOTH, expand=True)
        
        # Add scrollbar to status tree
        status_scrollbar = ttk.Scrollbar(self.status_frame, orient="vertical", command=self.status_tree.yview)
        self.status_tree.configure(yscrollcommand=status_scrollbar.set)
        status_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        # Log Frame
        log_frame = ttk.Frame(self.main_frame)
        log_frame.pack(fill=tk.BOTH, expand=True)
        
        self.log = tk.Text(log_frame, height=8, state='disabled', wrap='word')
        self.log.pack(fill=tk.BOTH, expand=True, pady=5)
        
        # Add a scrollbar to the log
        log_scrollbar = ttk.Scrollbar(self.log, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=log_scrollbar.set)
        log_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
    def apply_user_filters(self):
        """Apply filters to user list based on status"""
        # Get all users from tree
        all_items = self.user_tree.get_children()
        
        # Show all users if show_all_users is selected
        if self.show_all_users.get():
            for item in all_items:
                self.user_tree.item(item, tags=self.user_tree.item(item, "tags"))
        else:
            # Otherwise filter by status
            for item in all_items:
                values = self.user_tree.item(item, "values")
                status = values[3] if len(values) > 3 else ""
                
                # Hide offline users unless show_offline_users is selected
                if "Offline" in status and not self.show_offline_users.get():
                    self.user_tree.detach(item)
                else:
                    self.user_tree.move(item, "", "end")
        
        # Update user count
        self._update_user_count()
    
    def _update_user_count(self):
        """Update the user count label with total/online counts"""
        total_users = len(self.user_tree.get_children())
        online_users = 0
        
        for child in self.user_tree.get_children():
            values = self.user_tree.item(child, 'values')
            if len(values) > 3 and values[3] in ["Ready", "On Call"]:
                online_users += 1
                
        self.root.after(0, lambda: self.user_count_label.config(
            text=f"Users: {total_users} ({online_users} online)"
        ))
    
    def get_presence_status(self):
        """Request presence status for all users"""
        if not self.connected:
            messagebox.showwarning("Warning", "Not connected to TradeSense")
            return
            
        self.log_message("Requesting presence status for all users...")
        
        # Send presence request for each user in the tree
        threading.Thread(target=self._get_all_presence_thread, daemon=True).start()

    def _get_all_presence_thread(self):
        """Thread for requesting presence status for all users"""
        try:
            for child in self.user_tree.get_children():
                if not self.connected:
                    break
                    
                values = self.user_tree.item(child, 'values')
                login = values[0]
                
                # Skip users we already know are online
                if login in self.user_presence and self.user_presence[login] in ["LOGGED_IN", "ON_CALL"]:
                    continue
                    
                self._request_user_presence(login)
                
                # Small delay to avoid flooding the server
                time.sleep(0.1)
                
        except Exception as e:
            self.root.after(0, self.log_message, f"Error getting presence status: {str(e)}")
            
    def _request_user_presence(self, login):
        """Request presence status for a specific user"""
        try:
            command_ref = f"get_presence_{login}_{int(time.time())}"
            
            msg = {
                "command": "get_user_presence",
                "command_ref": command_ref,
                "args": {
                    "login": login
                }
            }
            
            self.ws.send(json.dumps(msg))
            
        except Exception as e:
            self.root.after(0, self.log_message, f"Error requesting presence for {login}: {str(e)}")
            
    def add_quick_number(self):
        """Add a new quick dial number"""
        num = simpledialog.askstring("Add Quick Dial", "Enter number:")
        if num and num not in self.quick_numbers:
            self.quick_numbers.append(num)
            self.update_quick_buttons()
            self.save_config()
            
    def update_quick_buttons(self):
        """Update quick dial buttons"""
        # Get the parent frame from the first button
        if self.quick_buttons:
            quick_frame = self.quick_buttons[0].master
            # Destroy existing buttons
            for btn in self.quick_buttons:
                btn.destroy()
        else:
            # Find the quick_frame in the widget hierarchy
            for child in self.main_frame.winfo_children():
                if isinstance(child, ttk.LabelFrame) and child['text'] == 'Quick Dial':
                    quick_frame = child
                    break
        
        self.quick_buttons = []
        for i, num in enumerate(self.quick_numbers):
            btn = ttk.Button(quick_frame, text=num, width=5,
                            command=lambda n=num: self.quick_dial(n))
            btn.grid(row=0, column=i, padx=2)
            self.quick_buttons.append(btn)
            
        ttk.Button(quick_frame, text="+", width=3,
                  command=self.add_quick_number).grid(row=0, column=len(self.quick_numbers), padx=5)
        
    def quick_dial(self, number):
        """Handle quick dial button clicks"""
        self.number.delete(0, tk.END)
        self.number.insert(0, number)
        self.start_dialing()
        
    def log_message(self, message):
        """Add a message to the log"""
        self.log.config(state='normal')
        self.log.insert(tk.END, f"{time.strftime('%H:%M:%S')} - {message}\n")
        self.log.config(state='disabled')
        self.log.see(tk.END)
        
    def connect(self):
        """Connect to TradeSense WBA"""
        url = self.ws_url.get()
        token = self.token.get()
        
        if not url or not token:
            messagebox.showerror("Error", "URL and Token are required")
            return
            
        self.save_config()
        threading.Thread(target=self._connect_thread, args=(url, token), daemon=True).start()
        
    def _connect_thread(self, url, token):
        """Thread for establishing WebSocket connection"""
        try:
            self.root.after(0, self.log_message, f"Attempting to connect to {url}")
            self.ws = create_connection(url)
            self.root.after(0, self.log_message, "WebSocket connection established")
            
            auth_msg = {
                "command": "auth",
                "command_ref": f"auth_{int(time.time())}",
                "args": {"token": token}
            }
            
            self.root.after(0, self.log_message, f"Sending auth request: {json.dumps(auth_msg)}")
            self.ws.send(json.dumps(auth_msg))
            
            raw_response = self.ws.recv()
            self.root.after(0, self.log_message, f"Auth response received: {raw_response}")
            
            response = json.loads(raw_response)
            
            if response.get("success"):
                self.connected = True
                self.last_auth_time = time.time()
                self.root.after(0, lambda: self.conn_status.config(
                    text="Connected",
                    foreground="green"
                ))
                self.root.after(0, self.log_message, "Connected successfully")
                
                # Start listening thread
                if not self.listen_thread_running:
                    self.listen_thread_running = True
                    threading.Thread(target=self._listen_thread, daemon=True).start()
                    
                self.fetch_users()
                self.subscribe_notifications()
                self.subscribe_presence()
            else:
                error = response.get("error", {})
                error_msg = f"Code: {error.get('code')}, Status: {error.get('status')}, Message: {error.get('message')}"
                self.root.after(0, self.log_message, f"Auth failed: {error_msg}")
                self.root.after(0, messagebox.showerror, "Auth Failed", error_msg)
                
        except Exception as e:
            self.root.after(0, self.log_message, f"Connection failed: {str(e)}")
            self.root.after(0, self.log_message, f"Traceback: {traceback.format_exc()}")
            self.root.after(0, messagebox.showerror, "Connection Failed", str(e))
            
    def disconnect(self):
        """Disconnect from TradeSense"""
        if self.ws:
            try:
                self.ws.close()
            except:
                pass
            self.ws = None
            
        self.connected = False
        self.calling = False
        self.conn_status.config(text="Disconnected", foreground="red")
        self.log_message("Disconnected from TradeSense")
        
        # Reset presence tracking
        self.user_presence = {}

    def on_site_selected(self, event):
        """Handle site selection from combobox"""
        site_name = self.site_combo.get()
        if site_name in self.sites:
            self.current_site = site_name
            site_data = self.sites[site_name]
            self.ws_url.delete(0, tk.END)
            self.ws_url.insert(0, site_data['url'])
            self.token.delete(0, tk.END)
            self.token.insert(0, site_data['token'])
            self.log_message(f"Loaded site: {site_name}")
            
    def add_site(self):
        """Add a new site configuration"""
        site_name = simpledialog.askstring("New Site", "Enter site name:")
        if site_name:
            self.sites[site_name] = {
                "url": self.ws_url.get(),
                "token": self.token.get()
            }
            self.update_site_combobox()
            self.save_config()
            self.log_message(f"Added site: {site_name}")
            
    def save_current_site(self):
        """Save the current site configuration"""
        site_name = self.site_combo.get()
        if site_name:
            self.sites[site_name] = {
                "url": self.ws_url.get(),
                "token": self.token.get()
            }
            self.save_config()
            self.log_message(f"Saved site: {site_name}")
            
    def delete_site(self):
        """Delete the selected site"""
        site_name = self.site_combo.get()
        if site_name and messagebox.askyesno("Confirm", f"Delete site '{site_name}'?"):
            del self.sites[site_name]
            self.update_site_combobox()
            self.save_config()
            self.log_message(f"Deleted site: {site_name}")
            
    def update_site_combobox(self):
        """Update the site selection combobox"""
        self.site_combo['values'] = list(self.sites.keys())
        if self.sites:
            self.site_combo.current(0)
            self.on_site_selected(None)
            
    def search_users(self):
        """Search for specific users"""
        if not self.connected:
            messagebox.showwarning("Warning", "Not connected to TradeSense")
            return
            
        search_term = self.search_var.get().strip()
        if not search_term:
            messagebox.showwarning("Warning", "Please enter a search term")
            return
            
        # Clear existing users
        self.root.after(0, self.user_tree.delete, *self.user_tree.get_children())
        
        # Start search in a new thread
        threading.Thread(target=self._search_users_thread, args=(search_term,), daemon=True).start()
    
    def clear_search(self):
        """Clear search and fetch all users"""
        self.search_var.set("")
        self.fetch_users()
    
    def fetch_users(self):
        """Fetch list of users from the API with pagination support"""
        if not self.connected:
            messagebox.showwarning("Warning", "Not connected to TradeSense")
            return
            
        # Clear existing users
        self.root.after(0, self.user_tree.delete, *self.user_tree.get_children())
        
        # Start fetching in a new thread
        threading.Thread(target=self._fetch_all_users_thread, daemon=True).start()
            
    def _search_users_thread(self, search_term):
        """Thread for searching users"""
        try:
            operator = self.search_operator.get()
            command_ref = f"search_users_{int(time.time())}"
            
            msg = {
                "command": "get_users",
                "command_ref": command_ref,
                "args": {
                    "search": search_term,
                    "operator": operator,
                    "get_lines_info": False
                }
            }
            
            self.root.after(0, self.log_message, f"Searching for users with '{search_term}' ({operator})")
            self.ws.send(json.dumps(msg))
            
            self.ws.settimeout(10.0)
            try:
                raw_response = self.ws.recv()
                response = json.loads(raw_response)
                
                if not response.get("success"):
                    error = response.get("error", {})
                    error_msg = f"Code: {error.get('code')}, Status: {error.get('status')}, Message: {error.get('message')}"
                    self.root.after(0, self.log_message, f"Search failed: {error_msg}")
                    return
                    
                data = response.get('data', {})
                users = data.get('users', [])
                
                self.root.after(0, self.log_message, f"Found {len(users)} users matching '{search_term}'")
                self.root.after(0, self._update_user_tree, users)
                
                # Get presence status for all fetched users
                self.root.after(0, self.get_presence_status)
                
            except socket.timeout:
                self.root.after(0, self.log_message, "Timeout waiting for search response")
                
        except Exception as e:
            self.root.after(0, self.log_message, f"Error searching users: {str(e)}")
            self.root.after(0, self.log_message, f"Traceback: {traceback.format_exc()}")
    
    def _fetch_all_users_thread(self):
        """Thread for fetching all users with pagination"""
        try:
            all_users = []
            
            # The API doesn't support pagination parameters, so we'll just do a basic request
            # The server will handle pagination and return current_batch/last_batch info
            command_ref = f"fetch_users_{int(time.time())}"
            
            msg = {
                "command": "get_users",
                "command_ref": command_ref,
                "args": {
                    "get_lines_info": False  # Set to False for better performance
                }
            }
            
            self.root.after(0, self.log_message, f"Sending get_users request: {json.dumps(msg)}")
            self.ws.send(json.dumps(msg))
            
            # Add timeout handling for recv
            self.ws.settimeout(30.0)  # 30 second timeout for large responses
            try:
                raw_response = self.ws.recv()
                # Truncate long responses for logging
                log_response = raw_response[:200] + "..." if len(raw_response) > 200 else raw_response
                self.root.after(0, self.log_message, f"Received response: {log_response}")
                
                response = json.loads(raw_response)
                
                if not response.get("success"):
                    error = response.get("error", {})
                    error_msg = f"Code: {error.get('code')}, Status: {error.get('status')}, Message: {error.get('message')}, Reason: {error.get('reason', '')}"
                    self.root.after(0, self.log_message, f"Failed to fetch users: {error_msg}")
                    return
                    
                # Extract data from response
                data = response.get('data', {})
                users = data.get('users', [])
                all_users.extend(users)
                
                # Get pagination info from response
                current_batch = data.get('current_batch', 1)
                last_batch = data.get('last_batch', 1)
                
                self.root.after(0, self.log_message, 
                              f"Fetched batch {current_batch}/{last_batch} with {len(users)} users")
                
                # Update UI with users
                self.root.after(0, self._update_user_tree, users)
                
                # If the system has many users (multiple batches), let the user know
                if current_batch < last_batch:
                    self.root.after(0, self.log_message, 
                                  "Warning: API returned partial results. Unable to fetch additional batches.")
                    
                # Get presence status for all fetched users
                self.root.after(1000, self.get_presence_status)
                    
            except socket.timeout:
                self.root.after(0, self.log_message, "Timeout waiting for user list response")
                
            self.root.after(0, self.log_message, f"Total users loaded: {len(all_users)}")
            
        except Exception as e:
            self.root.after(0, self.log_message, f"Error fetching users: {str(e)}")
            self.root.after(0, self.log_message, f"Traceback: {traceback.format_exc()}")
            
    def _update_user_tree(self, users):
        """Update the user tree with a batch of users"""
        for user in users:
            login = user.get('login', '')
            
            # Determine initial status based on presence info if available
            initial_status = "Unknown"
            initial_tags = ()
            
            if login in self.user_presence:
                presence_state = self.user_presence[login]
                if presence_state == "LOGGED_IN":
                    initial_status = "Ready"
                    initial_tags = ('online',)
                elif presence_state == "ON_CALL":
                    initial_status = "On Call"
                    initial_tags = ('busy',)
                elif presence_state == "LOGGED_OUT":
                    initial_status = "Offline"
                    initial_tags = ('offline',)
            
            self.user_tree.insert(
                "", "end",
                values=(
                    login,
                    f"{user.get('firstName', '')} {user.get('lastName', '')}",
                    user.get('turret', 'N/A'),
                    initial_status
                ),
                tags=initial_tags
            )
        
        # Update user count
        self._update_user_count()
        
        # Apply filters
        self.apply_user_filters()
            
    def save_config(self):
        """Save configuration to file"""
        config = {
            "sites": self.sites,
            "quick_numbers": self.quick_numbers,
            "current_site": self.current_site
        }
        with open(self.config_file, 'w') as f:
            json.dump(config, f)
            
    def load_config(self):
        """Load configuration from file"""
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r') as f:
                    config = json.load(f)
                    self.sites = config.get("sites", {})
                    self.quick_numbers = config.get("quick_numbers", ["100", "911", "999"])
                    self.current_site = config.get("current_site")
                    
                    if self.sites:
                        self.update_site_combobox()
                    if self.quick_numbers:
                        self.update_quick_buttons()
            except Exception as e:
                self.log_message(f"Error loading config: {str(e)}")

    def start_dialing(self):
        """Start the dialing process with the specified number and interval"""
        if not self.connected:
            messagebox.showwarning("Warning", "Not connected to TradeSense")
            return
            
        number = self.number.get()
        if not number:
            messagebox.showwarning("Warning", "Please enter a number to dial")
            return
            
        selected = self.user_tree.selection()
        if not selected:
            messagebox.showwarning("Warning", "No user(s) selected")
            return
            
        try:
            interval = int(self.interval.get())
            max_concurrent = int(self.max_concurrent.get())
        except ValueError:
            messagebox.showwarning("Warning", "Invalid interval or concurrent value")
            return
            
        self.calling = True
        self.start_button.config(state='disabled')
        
        if self.multi_dial_var.get():
            # Multi-user dialing
            users = []
            for item in selected:
                user_data = self.user_tree.item(item, 'values')
                login = user_data[0]
                status = user_data[3] if len(user_data) > 3 else "Unknown"
                
                # Skip offline users if the option is enabled
                if self.skip_offline_var.get() and status == "Offline":
                    self.log_message(f"Skipping offline user: {login}")
                    continue
                    
                users.append(login)
            
            if not users:
                messagebox.showwarning("Warning", "No online users selected for dialing")
                self.calling = False
                self.start_button.config(state='normal')
                return
                
            self.log_message(f"Started multi-user dialing for {len(users)} users to {number}")
            threading.Thread(target=self._multi_dialing_thread, 
                           args=(users, number, interval, max_concurrent), 
                           daemon=True).start()
        else:
            # Single user dialing
            user_data = self.user_tree.item(selected[0], 'values')
            login = user_data[0]
            status = user_data[3] if len(user_data) > 3 else "Unknown"
            
            # Check if user is offline and skip if option is enabled
            if self.skip_offline_var.get() and status == "Offline":
                messagebox.showwarning("Warning", f"User {login} appears to be offline. Dialing canceled.")
                self.calling = False
                self.start_button.config(state='normal')
                return
                
            self.log_message(f"Started dialing {number} for {login} every {interval} seconds")
            threading.Thread(target=self._dialing_thread, 
                           args=(login, number, interval), 
                           daemon=True).start()
        
    def stop_dialing(self):
        """Stop the dialing process"""
        self.calling = False
        self.start_button.config(state='normal')
        self.log_message("Stopped dialing")
        
    def _dialing_thread(self, login, number, interval):
        """Thread for handling single user dialing process"""
        while self.calling and self.connected:
            try:
                device = self.device_combo.get() if self.device_combo.get() else None
                
                args = {
                    "action": "place",
                    "login": login,
                    "remote_extension": number
                }
                
                # Add device if specified
                if device:
                    args["device"] = device
                
                msg = {
                    "command": "service_ctd",
                    "command_ref": f"dial_{int(time.time())}",
                    "args": args
                }
                
                self.root.after(0, self.log_message, f"Sending dial request for {login} to {number}")
                self.ws.send(json.dumps(msg))
                
                # Set a timeout for receiving response
                self.ws.settimeout(5.0)
                try:
                    raw_response = self.ws.recv()
                    response = json.loads(raw_response)
                    
                    if response.get("success"):
                        data = response.get('data', {})
                        call_ref = data.get('call_ref', 'unknown')
                        turret = data.get('turret', 'unknown')
                        local_extension = data.get('local_extension', 'unknown')
                        
                        self.root.after(0, self.log_message, 
                                      f"Call placed successfully - User: {login}, Turret: {turret}, Call Ref: {call_ref}, Local Ext: {local_extension}")
                        
                        self.dial_stats["success"] += 1
                        self.root.after(0, self._update_stats)
                        
                        self.recent_calls.append({
                            "time": time.strftime("%H:%M:%S"),
                            "user": login,
                            "number": number,
                            "status": f"Success (Ref: {call_ref})",
                            "call_ref": call_ref,
                            "turret": turret,
                            "local_extension": local_extension
                        })
                        self.root.after(0, self._update_status_tab)
                        
                        # Update user status to on call
                        self._update_user_status(login, "On Call")
                    else:
                        error = response.get("error", {})
                        error_msg = f"Code: {error.get('code')}, Status: {error.get('status')}, Message: {error.get('message')}"
                        self.root.after(0, self.log_message, f"Failed to dial {number}: {error_msg}")
                        
                        self.dial_stats["failed"] += 1
                        self.root.after(0, self._update_stats)
                        
                        self.recent_calls.append({
                            "time": time.strftime("%H:%M:%S"),
                            "user": login,
                            "number": number,
                            "status": f"Failed: {error.get('message', error.get('status', 'Unknown error'))}"
                        })
                        self.root.after(0, self._update_status_tab)
                
                except socket.timeout:
                    self.root.after(0, self.log_message, f"Timeout waiting for dial response")
                    self.dial_stats["failed"] += 1
                    self.root.after(0, self._update_stats)
                    
                    self.recent_calls.append({
                        "time": time.strftime("%H:%M:%S"),
                        "user": login,
                        "number": number,
                        "status": "Failed: Timeout"
                    })
                    self.root.after(0, self._update_status_tab)
                    
            except Exception as e:
                self.root.after(0, self.log_message, f"Error during dialing: {str(e)}")
                self.root.after(0, self.log_message, f"Traceback: {traceback.format_exc()}")
                self.root.after(0, self.stop_dialing)
                return
                
            # Reset timeout for the listen thread
            if self.ws:
                self.ws.settimeout(None)
                
            # Wait for the specified interval
            for _ in range(interval):
                if not self.calling:
                    break
                time.sleep(1)
    
    def _multi_dialing_thread(self, users, number, interval, max_concurrent):
        """Thread for handling multiple user dialing with concurrency control"""
        while self.calling and self.connected:
            try:
                # Use ThreadPoolExecutor to manage concurrent calls
                with ThreadPoolExecutor(max_workers=max_concurrent) as executor:
                    # Create futures for all users
                    futures = []
                    for login in users:
                        if not self.calling:
                            break
                            
                        # Skip users that are offline if option is enabled
                        if self.skip_offline_var.get():
                            status = self._get_user_status(login)
                            if status == "Offline":
                                self.root.after(0, self.log_message, f"Skipping offline user: {login}")
                                continue
                                
                        future = executor.submit(self._make_single_call, login, number)
                        futures.append((login, future))
                    
                    # Wait for all calls to complete
                    for login, future in futures:
                        if not self.calling:
                            break
                        try:
                            result = future.result(timeout=10.0)
                        except Exception as e:
                            self.root.after(0, self.log_message, f"Error calling for {login}: {str(e)}")
                
                # Wait for interval before next round
                for _ in range(interval):
                    if not self.calling:
                        return
                    time.sleep(1)
                    
            except Exception as e:
                self.root.after(0, self.log_message, f"Error in multi-dialing: {str(e)}")
                self.root.after(0, self.stop_dialing)
                return
                
    def _get_user_status(self, login):
        """Get the current status of a user from the tree"""
        for child in self.user_tree.get_children():
            values = self.user_tree.item(child, 'values')
            if values[0] == login:
                return values[3] if len(values) > 3 else "Unknown"
        return "Unknown"
    
    def _make_single_call(self, login, number):
        """Make a single call for a user"""
        try:
            device = self.device_combo.get() if self.device_combo.get() else None
            
            args = {
                "action": "place",
                "login": login,
                "remote_extension": number
            }
            
            if device:
                args["device"] = device
            
            msg = {
                "command": "service_ctd",
                "command_ref": f"dial_{int(time.time())}_{login}",
                "args": args
            }
            
            self.root.after(0, self.log_message, f"Calling {number} for {login}")
            self.ws.send(json.dumps(msg))
            
            # Set a timeout for receiving response
            self.ws.settimeout(5.0)
            
            raw_response = self.ws.recv()
            response = json.loads(raw_response)
            
            if response.get("success"):
                data = response.get('data', {})
                call_ref = data.get('call_ref', 'unknown')
                turret = data.get('turret', 'unknown')
                local_extension = data.get('local_extension', 'unknown')
                
                self.dial_stats["success"] += 1
                self.root.after(0, self._update_stats)
                
                self.recent_calls.append({
                    "time": time.strftime("%H:%M:%S"),
                    "user": login,
                    "number": number,
                    "status": f"Success (Ref: {call_ref})",
                    "call_ref": call_ref,
                    "turret": turret,
                    "local_extension": local_extension
                })
                self.root.after(0, self._update_status_tab)
                
                # Update user status to on call
                self._update_user_status(login, "On Call")
                
                return True
            else:
                error = response.get("error", {})
                error_msg = error.get('message', error.get('status', 'Unknown error'))
                
                self.dial_stats["failed"] += 1
                self.root.after(0, self._update_stats)
                
                self.recent_calls.append({
                    "time": time.strftime("%H:%M:%S"),
                    "user": login,
                    "number": number,
                    "status": f"Failed: {error_msg}"
                })
                self.root.after(0, self._update_status_tab)
                
                return False
                
        except socket.timeout:
            self.dial_stats["failed"] += 1
            self.root.after(0, self._update_stats)
            
            self.recent_calls.append({
                "time": time.strftime("%H:%M:%S"),
                "user": login,
                "number": number,
                "status": "Failed: Timeout"
            })
            self.root.after(0, self._update_status_tab)
            
            return False
            
        except Exception as e:
            self.root.after(0, self.log_message, f"Error making call for {login}: {str(e)}")
            return False
            
        finally:
            # Reset timeout for the listen thread
            if self.ws:
                self.ws.settimeout(None)
        auth_msg = {
                "command": "auth",
                "command_ref": f"reauth_{int(time.time())}",
                "args": {"token": self.token.get()}
            }
            self.root.after(0, self.log_message, f"Sending reauth request: {json.dumps(auth_msg)}")
            self.ws.send(json.dumps(auth_msg))
            
            self.ws.settimeout(10.0)  # 10 second timeout for reauth
            raw_response = self.ws.recv()
            self.root.after(0, self.log_message, f"Reauth response: {raw_response}")
            
            response = json.loads(raw_response)
            
            if response.get("success"):
                self.last_auth_time = time.time()
                self.root.after(0, self.log_message, "Re-authenticated successfully")
            else:
                error = response.get("error", {})
                error_msg = f"Code: {error.get('code')}, Status: {error.get('status')}, Message: {error.get('message')}"
                self.root.after(0, self.log_message, f"Re-authentication failed: {error_msg}")
                
                # If token is invalid, try to disconnect and reconnect
                if error.get("code") == 498:  # Invalid Token
                    self.root.after(0, self.log_message, "Invalid token - will disconnect")
                    self.root.after(0, self.disconnect)
                
            # Reset timeout for the listen thread
            if self.ws:
                self.ws.settimeout(None)
                
        except Exception as e:
            self.root.after(0, self.log_message, f"Error during re-authentication: {str(e)}")
            self.root.after(0, self.log_message, f"Traceback: {traceback.format_exc()}")
            
            # Try to reset timeout for the listen thread
            try:
                if self.ws:
                    self.ws.settimeout(None)
            except:
                pass
                
    def wait_for_response(self, command_ref, timeout=5):
        """Wait for response with matching command_ref"""
        q = queue.Queue()
        self.pending_responses[command_ref] = q
        try:
            return q.get(timeout=timeout)
        except queue.Empty:
            self.log_message(f"Timeout waiting for response to {command_ref}")
            return None
        finally:
            self.pending_responses.pop(command_ref, None)

if __name__ == "__main__":
    root = tk.Tk()
    app = TradeSenseDialer(root)
    root.mainloop()