from tau_bench.types import Task, Action

TASKS_TRAIN = [
    Task(
        annotator="synthetic",
        user_id="sofia_rossi_8776",
        instruction="Your name is Sofia Rossi and your email is sofia.rossi2645@example.com. You are dependent, rigid, creative, confident, relaxing. For #W2818151, modify Luggage Set {'piece count': '4-piece', 'color': 'red', 'material': 'hardshell'} to {'piece count': '3-piece', 'color': 'blue', 'material': 'softshell'}; via credit_card_5051208. Cancel order #W5500815 because ordered by mistake. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W2818151",
                    "item_ids": ["9956648681"],
                    "new_item_ids": ["6301799585"],
                    "payment_method_id": "credit_card_5051208",
                },
            ),
            Action(
                name="cancel_pending_order",
                kwargs={"order_id": "#W5500815", "reason": "ordered by mistake"},
            ),
        ],
        outputs=[],
    
        uuid="d53b77e9-9ef1-4289-ae9e-25f31fcff6ac",),
    Task(
        annotator="synthetic",
        user_id="raj_lopez_5873",
        instruction="Your name is Raj Lopez and your email is raj.lopez2997@example.com. You are polite, logical, dependent. Cancel order #W7162915 because ordered by mistake. For #W5107138, modify Hiking Boots {'size': '7', 'material': 'synthetic', 'waterproof': 'no'} to {}; via paypal_7007375. ",
        actions=[
            Action(
                name="cancel_pending_order",
                kwargs={"order_id": "#W7162915", "reason": "ordered by mistake"},
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W5107138",
                    "item_ids": ["1437889264"],
                    "new_item_ids": ["1437889264"],
                    "payment_method_id": "paypal_7007375",
                },
            ),
        ],
        outputs=[],
    
        uuid="1fe5efec-cc05-4315-aefd-50475d9ab525",),
    Task(
        annotator="synthetic",
        user_id="evelyn_lopez_5487",
        instruction="Your name is Evelyn Lopez and your email is evelyn.lopez6910@example.com. You are organized, sad, confident. For #W3007862, modify Grill {'type': 'electric', 'size': 'medium', 'features': 'side burner'} to {'type': 'gas', 'size': 'portable'}; via credit_card_3566337. Cancel order #W1890669 because ordered by mistake. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W3007862",
                    "item_ids": ["5666020311"],
                    "new_item_ids": ["9724317332"],
                    "payment_method_id": "credit_card_3566337",
                },
            ),
            Action(
                name="cancel_pending_order",
                kwargs={"order_id": "#W1890669", "reason": "ordered by mistake"},
            ),
        ],
        outputs=[],
    
        uuid="bab90bdd-9c89-4fc0-9663-c51e0685a387",),
    Task(
        annotator="synthetic",
        user_id="daiki_johnson_9523",
        instruction="Your name is Daiki Johnson and your email is daiki.johnson2279@example.com. You are optimistic, direct, rigid, sad. Cancel order #W1436802 because no longer needed. For #W5282037, modify Garden Hose {'length': '25ft', 'material': 'latex', 'color': 'green'} to {'material': 'vinyl', 'color': 'blue'}; via paypal_2433177. ",
        actions=[
            Action(
                name="cancel_pending_order",
                kwargs={"order_id": "#W1436802", "reason": "no longer needed"},
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W5282037",
                    "item_ids": ["3230708338"],
                    "new_item_ids": ["9829827210"],
                    "payment_method_id": "paypal_2433177",
                },
            ),
        ],
        outputs=[],
        uuid="02c47c8b-ceee-433a-8d17-406053aeb640",),
    Task(
        annotator="synthetic",
        user_id="sofia_thomas_1518",
        instruction="Your name is Sofia Thomas and your zip code is 75307. You are creative, independent, cautious, rigid, organized. For #W2297866, modify Vacuum Cleaner {'type': 'upright', 'bagged/bagless': 'bagless', 'features': 'HEPA filter'} to {'type': 'robotic', 'bagged/bagless': 'bagged', 'features': 'cordless'}; via paypal_5334408. Cancel order #W7619352 because ordered by mistake. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W2297866",
                    "item_ids": ["7407609582"],
                    "new_item_ids": ["4602305039"],
                    "payment_method_id": "paypal_5334408",
                },
            ),
            Action(
                name="cancel_pending_order",
                kwargs={"order_id": "#W7619352", "reason": "ordered by mistake"},
            ),
        ],
        outputs=[],
    
        uuid="2f57f421-94ea-4a1a-a0c0-976124c96465",),
    Task(
        annotator="synthetic",
        user_id="yara_muller_8652",
        instruction="Your name is Yara Muller and your email is yara.muller9246@example.com. You are rigid, shy, confident. Cancel order #W5056519 because no longer needed. For #W5995614, modify Dumbbell Set {'weight range': '5-25 lbs', 'material': 'iron', 'set type': 'adjustable'} to {'weight range': '30-50 lbs', 'material': 'rubber'}; Luggage Set {'piece count': '3-piece', 'color': 'black', 'material': 'softshell'} to {'piece count': '2-piece'}; via credit_card_3095586. ",
        actions=[
            Action(
                name="cancel_pending_order",
                kwargs={"order_id": "#W5056519", "reason": "no longer needed"},
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W5995614",
                    "item_ids": ["3877338112", "9692325258"],
                    "new_item_ids": ["3735133539", "8926329222"],
                    "payment_method_id": "credit_card_3095586",
                },
            ),
        ],
        outputs=[],
    
        uuid="0d14ec95-969d-4ded-9161-db46eeeb5b78",),
    Task(
        annotator="synthetic",
        user_id="ethan_lopez_6291",
        instruction="Your name is Ethan Lopez and your email is ethan.lopez8943@example.com. You are cautious, relaxing. For #W8073920, modify Hiking Boots {'size': '12', 'material': 'leather', 'waterproof': 'yes'} to {'size': '7', 'material': 'synthetic', 'waterproof': 'no'}; via gift_card_7219486. Cancel order #W6779827 because no longer needed. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W8073920",
                    "item_ids": ["8277474082"],
                    "new_item_ids": ["1437889264"],
                    "payment_method_id": "gift_card_7219486",
                },
            ),
            Action(
                name="cancel_pending_order",
                kwargs={"order_id": "#W6779827", "reason": "no longer needed"},
            ),
        ],
        outputs=[],
    
        uuid="48ecd30a-b09b-44d6-839f-631296644617",),
    Task(
        annotator="synthetic",
        user_id="ivan_khan_7475",
        instruction="Your name is Ivan Khan and your zip code is 28243. You are confident, organized, creative, busy. Cancel order #W5782623 because ordered by mistake. For #W5270061, modify Desk Lamp {'color': 'silver', 'brightness': 'low', 'power source': 'battery'} to {'color': 'black', 'brightness': 'medium', 'power source': 'AC adapter'}; via paypal_7729105. ",
        actions=[
            Action(
                name="cancel_pending_order",
                kwargs={"order_id": "#W5782623", "reason": "ordered by mistake"},
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W5270061",
                    "item_ids": ["7453605304"],
                    "new_item_ids": ["5320792178"],
                    "payment_method_id": "paypal_7729105",
                },
            ),
        ],
        outputs=[],
    
        uuid="68fafc10-5151-4ac5-8e48-6d6547019d38",),
    Task(
        annotator="synthetic",
        user_id="amelia_patel_7834",
        instruction="Your name is Amelia Patel and your zip code is 85051. You are messy, impatient, relaxing. Cancel order #W9077472 because ordered by mistake. For #W2079779, modify Sunglasses {'frame color': 'black', 'lens color': 'brown', 'lens type': 'polarized', 'frame material': 'plastic'} to {'frame color': 'silver', 'lens color': 'blue', 'lens type': 'non-polarized'}; Action Camera {'resolution': '1080p', 'waterproof': 'no', 'color': 'black'} to {'resolution': '5K'}; via gift_card_3751659. ",
        actions=[
            Action(
                name="cancel_pending_order",
                kwargs={"order_id": "#W9077472", "reason": "ordered by mistake"},
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W2079779",
                    "item_ids": ["4358482460", "9168994198"],
                    "new_item_ids": ["4329558751", "7523669277"],
                    "payment_method_id": "gift_card_3751659",
                },
            ),
        ],
        outputs=[],
    
        uuid="08c7900d-224e-478e-aa6f-1b5a931d0100",),
    Task(
        annotator="synthetic",
        user_id="yusuf_ahmed_6232",
        instruction="Your name is Yusuf Ahmed and your email is yusuf.ahmed5476@example.com. You are organized, confident, busy, dependent, logical. Cancel order #W7007896 because ordered by mistake. For #W7756209, modify Grill {'type': 'electric', 'size': 'large', 'features': 'rotisserie'} to {'type': 'gas', 'size': 'portable', 'features': 'side burner'}; Backpack {'color': 'grey', 'size': 'small', 'material': 'nylon', 'compartment': 'laptop'} to {'size': 'large', 'material': 'polyester', 'compartment': 'hydration'}; via credit_card_2167533. ",
        actions=[
            Action(
                name="cancel_pending_order",
                kwargs={"order_id": "#W7007896", "reason": "ordered by mistake"},
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W7756209",
                    "item_ids": ["4404981319", "8054888773"],
                    "new_item_ids": ["9724317332", "6309044598"],
                    "payment_method_id": "credit_card_2167533",
                },
            ),
        ],
        outputs=[],
    
        uuid="eaf9ba14-bf52-4bbc-9d0b-f02d09379fac",),
    
]