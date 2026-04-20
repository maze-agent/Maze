from tau_bench.types import Task, Action

TASKS_TRAIN = [
    Task(
        annotator="synthetic",
        user_id="harper_thomas_9402",
        instruction="Your name is Harper Thomas and your email is harper.thomas1454@example.com. You are logical, dependent, impatient, busy. For #W7425646, change payment to credit_card_1199336. For #W7425646, modify Smart Thermostat {'compatibility': 'Apple HomeKit', 'color': 'black'} to {}; via credit_card_1283450. ",
        actions=[
            Action(
                name="modify_pending_order_payment",
                kwargs={
                    "order_id": "#W7425646",
                    "payment_method_id": "credit_card_1199336",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W7425646",
                    "item_ids": ["4983901480"],
                    "new_item_ids": ["4983901480"],
                    "payment_method_id": "credit_card_1283450",
                },
            ),
        ],
        outputs=[],
    
        uuid="faff32d4-f4cf-4ff7-a504-2ea31f58830f",),
    Task(
        annotator="synthetic",
        user_id="lucas_martin_4549",
        instruction="Your name is Lucas Martin and your email is lucas.martin5733@example.com. You are patient, cautious, organized. For #W9318778, change payment to credit_card_7862034. For #W9318778, modify Bicycle {'frame size': 'medium', 'color': 'black', 'type': 'mountain'} to {'frame size': 'large', 'color': 'red'}; Air Purifier {'room size': 'medium', 'filter type': 'HEPA', 'features': 'quiet operation'} to {}; via credit_card_7862034. ",
        actions=[
            Action(
                name="modify_pending_order_payment",
                kwargs={
                    "order_id": "#W9318778",
                    "payment_method_id": "credit_card_7862034",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W9318778",
                    "item_ids": ["2143041831", "3076708684"],
                    "new_item_ids": ["5606522780", "3076708684"],
                    "payment_method_id": "credit_card_7862034",
                },
            ),
        ],
        outputs=[],
    
        uuid="1e2cd64e-66bb-480c-9583-82b8897b5c1a",),
    Task(
        annotator="synthetic",
        user_id="aarav_brown_3744",
        instruction="Your name is Aarav Brown and your email is aarav.brown3708@example.com. You are busy, patient. For #W5065081, modify Water Bottle {'capacity': '750ml', 'material': 'glass', 'color': 'black'} to {'capacity': '500ml', 'material': 'stainless steel', 'color': 'green'}; via credit_card_3627996. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W5065081",
                    "item_ids": ["4579334072"],
                    "new_item_ids": ["7533802601"],
                    "payment_method_id": "credit_card_3627996",
                },
            )
        ],
        outputs=[],
    
        uuid="a89bac30-fbc5-407d-a7f8-9b4603b44260",),
    Task(
        annotator="synthetic",
        user_id="ethan_sanchez_7289",
        instruction="Your name is Ethan Sanchez and your email is ethan.sanchez3299@example.com. You are pessimistic, shy, curious, relaxing. For #W7147989, modify Grill {'type': 'electric', 'size': 'portable', 'features': 'none'} to {'features': 'rotisserie'}; via gift_card_5917510. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W7147989",
                    "item_ids": ["1120917161"],
                    "new_item_ids": ["5745575001"],
                    "payment_method_id": "gift_card_5917510",
                },
            )
        ],
        outputs=[],
    
        uuid="c75f3d11-aec1-4543-921d-d625b3f80660",),
    Task(
        annotator="synthetic",
        user_id="aarav_santos_4279",
        instruction="Your name is Aarav Santos and your email is aarav.santos2789@example.com. You are patient, pessimistic, insecure. For #W6111820, modify Wireless Earbuds {'color': 'blue', 'battery life': '4 hours', 'water resistance': 'IPX7'} to {'battery life': '8 hours', 'water resistance': 'IPX4'}; via credit_card_3816099. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W6111820",
                    "item_ids": ["2757705742"],
                    "new_item_ids": ["8555936349"],
                    "payment_method_id": "credit_card_3816099",
                },
            )
        ],
        outputs=[],
    
        uuid="68a99eef-d308-445a-9ebb-135112796aad",),
    Task(
        annotator="synthetic",
        user_id="aarav_santos_2259",
        instruction="Your name is Aarav Santos and your email is aarav.santos8320@example.com. You are insecure, polite, happy. For #W9672333, modify Vacuum Cleaner {'type': 'canister', 'bagged/bagless': 'bagged', 'features': 'cordless'} to {}; via paypal_7664977. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W9672333",
                    "item_ids": ["1345513440"],
                    "new_item_ids": ["1345513440"],
                    "payment_method_id": "paypal_7664977",
                },
            )
        ],
        outputs=[],
    
        uuid="b49ef441-7039-41ad-a161-813aac15cb62",),
    Task(
        annotator="synthetic",
        user_id="noah_taylor_8533",
        instruction="Your name is Noah Taylor and your zip code is 85010. You are relaxing, impatient, insecure, direct. For #W2286993, modify Skateboard {'deck material': 'bamboo', 'length': '31 inch', 'design': 'plain'} to {'deck material': 'plastic', 'design': 'custom'}; via gift_card_5354170. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W2286993",
                    "item_ids": ["4293355847"],
                    "new_item_ids": ["5038485381"],
                    "payment_method_id": "gift_card_5354170",
                },
            )
        ],
        outputs=[],
    
        uuid="210959d5-d080-4b71-a976-6c8b89ba0c38",),
    Task(
        annotator="synthetic",
        user_id="juan_rossi_6696",
        instruction="Your name is Juan Rossi and your zip code is 77209. You are independent, shy, curious, relaxing. For #W7602708, change payment to gift_card_8893815. For #W7602708, modify Garden Hose {'length': '25ft', 'material': 'vinyl', 'color': 'green'} to {'color': 'blue'}; via gift_card_8893815. ",
        actions=[
            Action(
                name="modify_pending_order_payment",
                kwargs={
                    "order_id": "#W7602708",
                    "payment_method_id": "gift_card_8893815",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W7602708",
                    "item_ids": ["3369928769"],
                    "new_item_ids": ["9829827210"],
                    "payment_method_id": "gift_card_8893815",
                },
            ),
        ],
        outputs=[],
    
        uuid="61c8273b-eec6-4f65-a191-9fa265cf394c",),
    
    Task(
        annotator="synthetic",
        user_id="juan_lopez_5820",
        instruction="Your name is Juan Lopez and your zip code is 85060. You are organized, direct, sad, optimistic, curious. For #W3386832, change address to {'order_id': '#W3386832', 'address1': '411 Park Avenue', 'address2': 'Suite 987', 'city': 'Phoenix', 'country': 'USA', 'state': 'AZ', 'zip': '85060'} (same as #W3700848). For #W3386832, modify Cycling Helmet {'size': 'M', 'color': 'blue', 'ventilation': 'low'} to {'ventilation': 'high'}; Espresso Machine {'pressure': '9 bar', 'capacity': '2L', 'type': 'automatic'} to {'capacity': '1L', 'type': 'manual'}; Garden Hose {'length': '50ft', 'material': 'vinyl', 'color': 'green'} to {'material': 'latex', 'color': 'black'}; via paypal_6729210. ",
        actions=[
            Action(
                name="modify_pending_order_address",
                kwargs={
                    "order_id": "#W3386832",
                    "address1": "411 Park Avenue",
                    "address2": "Suite 987",
                    "city": "Phoenix",
                    "country": "USA",
                    "state": "AZ",
                    "zip": "85060",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W3386832",
                    "item_ids": ["3339188619", "3709608322", "8249784860"],
                    "new_item_ids": ["9013366374", "7407838442", "4024196380"],
                    "payment_method_id": "paypal_6729210",
                },
            ),
        ],
        outputs=[],
    
        uuid="aac399af-c0ea-40ce-80b2-113fd3494747",),
    Task(
        annotator="synthetic",
        user_id="fatima_johnson_7581",
        instruction="Your name is Fatima Johnson and your email is fatima.johnson2300@example.com. You are creative, happy, curious, polite, impatient. For #W5199551, change payment to gift_card_1675628. For #W5199551, modify Cycling Helmet {'size': 'S', 'color': 'black', 'ventilation': 'medium'} to {'color': 'red', 'ventilation': 'low'}; Wristwatch {'strap material': 'silicone', 'dial color': 'black'} to {'strap material': 'metal', 'dial color': 'white'}; via paypal_5364164. ",
        actions=[
            Action(
                name="modify_pending_order_payment",
                kwargs={
                    "order_id": "#W5199551",
                    "payment_method_id": "gift_card_1675628",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W5199551",
                    "item_ids": ["5537798301", "1994478369"],
                    "new_item_ids": ["3358616356", "2407258246"],
                    "payment_method_id": "paypal_5364164",
                },
            ),
        ],
        outputs=[],
    
        uuid="b991b184-e25a-42e6-8d42-7b0a7e2eefca",),
    Task(
        annotator="synthetic",
        user_id="mason_kovacs_7590",
        instruction="Your name is Mason Kovacs and your zip code is 98137. You are direct, logical. For #W6030855, modify Bluetooth Speaker {'color': 'black', 'battery life': '20 hours', 'water resistance': 'no'} to {'color': 'red'}; via credit_card_4314033. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W6030855",
                    "item_ids": ["5650803029"],
                    "new_item_ids": ["1052700637"],
                    "payment_method_id": "credit_card_4314033",
                },
            )
        ],
        outputs=[],
    
        uuid="4eb95044-10d9-4a15-afaf-1de2ac26f6e6",),
    
    Task(
        annotator="synthetic",
        user_id="isabella_santos_1643",
        instruction="Your name is Isabella Santos and your email is isabella.santos9317@example.com. You are optimistic, confident, flexible. For #W9667707, change address to {'order_id': '#W9667707', 'address1': '967 Sunset Drive', 'address2': 'Suite 613', 'city': 'Fort Worth', 'country': 'USA', 'state': 'TX', 'zip': '76176'} (same as #W1654332). For #W9667707, modify Running Shoes {'size': '9', 'color': 'white', 'material': 'mesh', 'sole': 'rubber'} to {'color': 'black', 'material': 'synthetic'}; E-Reader {'screen size': '8-inch', 'connectivity': 'Wi-Fi', 'storage': '32GB'} to {'screen size': '7-inch', 'connectivity': 'Wi-Fi + Cellular'}; via credit_card_4056740. ",
        actions=[
            Action(
                name="modify_pending_order_address",
                kwargs={
                    "order_id": "#W9667707",
                    "address1": "967 Sunset Drive",
                    "address2": "Suite 613",
                    "city": "Fort Worth",
                    "country": "USA",
                    "state": "TX",
                    "zip": "76176",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W9667707",
                    "item_ids": ["9635758562", "7609274509"],
                    "new_item_ids": ["4107812777", "4273929280"],
                    "payment_method_id": "credit_card_4056740",
                },
            ),
        ],
        outputs=[],
    
        uuid="9e9d7820-207e-4b6b-a9bf-1b49513f1972",),
    Task(
        annotator="synthetic",
        user_id="ivan_santos_7021",
        instruction="Your name is Ivan Santos and your email is ivan.santos5925@example.com. You are happy, independent, polite, patient, busy. For #W5801125, modify Tea Kettle {'material': 'glass', 'capacity': '1.5 liters', 'stovetop compatibility': 'gas'} to {'material': 'ceramic', 'stovetop compatibility': 'induction'}; via paypal_5543657. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W5801125",
                    "item_ids": ["9647374798"],
                    "new_item_ids": ["3312883418"],
                    "payment_method_id": "paypal_5543657",
                },
            )
        ],
        outputs=[],
    
        uuid="030a8024-7aac-4e8c-9e1a-a24599d7445f",),
    Task(
        annotator="synthetic",
        user_id="raj_lee_3061",
        instruction="Your name is raj_lee_3061 and your email is raj.lee6137@example.com and your zip code is 75368. You are rigid, busy, logical, confident, happy. For #W9933266, modify Pet Bed {'size': 'small', 'material': 'fleece', 'color': 'brown'} to {'size': 'medium', 'color': 'grey'}; Yoga Mat {'thickness': '4mm', 'material': 'PVC', 'color': 'blue'} to {'thickness': '6mm', 'color': 'green'}; via paypal_4133936. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W9933266",
                    "item_ids": ["4537595158", "5586947715"],
                    "new_item_ids": ["6857426243", "7510236436"],
                    "payment_method_id": "paypal_4133936",
                },
            )
        ],
        outputs=[],
    
        uuid="0f5a568a-080d-4a65-adc2-3634f8b003e6",),
    Task(
        annotator="synthetic",
        user_id="sophia_martin_8570",
        instruction="Your name is Sophia Martin and your email is sophia.martin4832@example.com. You are optimistic, messy, creative. For #W1092119, change address to {'order_id': '#W1092119', 'address1': '760 Elm Avenue', 'address2': 'Suite 564', 'city': 'Houston', 'country': 'USA', 'state': 'TX', 'zip': '77034'} (same as #W1603792). For #W1092119, modify Luggage Set {'piece count': '3-piece', 'color': 'silver', 'material': 'softshell'} to {'piece count': '4-piece', 'color': 'blue'}; via credit_card_5694100. ",
        actions=[
            Action(
                name="modify_pending_order_address",
                kwargs={
                    "order_id": "#W1092119",
                    "address1": "760 Elm Avenue",
                    "address2": "Suite 564",
                    "city": "Houston",
                    "country": "USA",
                    "state": "TX",
                    "zip": "77034",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W1092119",
                    "item_ids": ["6690069155"],
                    "new_item_ids": ["8759627937"],
                    "payment_method_id": "credit_card_5694100",
                },
            ),
        ],
        outputs=[],
    
        uuid="b0110690-01b6-4307-a4af-9d8f3af65c71",),
    Task(
        annotator="synthetic",
        user_id="aarav_lee_1982",
        instruction="Your name is Aarav Lee and your email is aarav.lee6460@example.com. You are optimistic, happy, independent, patient. For #W3586556, modify Tablet {'screen size': '8-inch', 'storage': '128GB', 'color': 'gold'} to {'screen size': '7-inch', 'color': 'black'}; via credit_card_1640996. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W3586556",
                    "item_ids": ["6065192424"],
                    "new_item_ids": ["4913411651"],
                    "payment_method_id": "credit_card_1640996",
                },
            )
        ],
        outputs=[],
    
        uuid="391c06bc-8c83-46c0-a7db-e1eb31fb397a",),
    Task(
        annotator="synthetic",
        user_id="ethan_thomas_1791",
        instruction="Your name is Ethan Thomas and your email is ethan.thomas7730@example.com. You are patient, relaxing, rigid, logical, messy. For #W8465042, modify Smartphone {'color': 'gold', 'storage': '128GB', 'RAM': '4GB', 'screen size': '5.8-inch'} to {'color': 'black', 'RAM': '8GB'}; Smart Watch {'color': 'black', 'band material': 'silicone', 'display': 'AMOLED'} to {'color': 'gold'}; via paypal_6982172. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W8465042",
                    "item_ids": ["9929635042", "4920090458"],
                    "new_item_ids": ["1507389580", "2681513500"],
                    "payment_method_id": "paypal_6982172",
                },
            )
        ],
        outputs=[],
    
        uuid="c5ccd21f-94b8-4714-a0bb-44ccec9cb56e",),
    Task(
        annotator="synthetic",
        user_id="mei_kovacs_8020",
        instruction="Your name is Mei Kovacs and your zip code is 28236. You are dependent, rigid, relaxing. For #W7800651, modify Gaming Mouse {'color': 'RGB', 'sensor type': 'optical', 'connectivity': 'wired'} to {'color': 'black', 'sensor type': 'laser'}; via paypal_7644869. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W7800651",
                    "item_ids": ["5796612084"],
                    "new_item_ids": ["2193628750"],
                    "payment_method_id": "paypal_7644869",
                },
            )
        ],
        outputs=[],
    
        uuid="399b271d-9861-4cc7-8cb2-bb3c4be055d0",),
    Task(
        annotator="synthetic",
        user_id="emma_kovacs_9839",
        instruction="Your name is Emma Kovacs and your zip code is 32190. You are dependent, relaxing, curious. For #W8661412, modify Office Chair {'material': 'fabric', 'color': 'black', 'armrest': 'fixed', 'backrest height': 'standard'} to {'color': 'gray', 'armrest': 'none', 'backrest height': 'high-back'}; Water Bottle {'capacity': '500ml', 'material': 'stainless steel', 'color': 'black'} to {'capacity': '750ml', 'material': 'plastic'}; via credit_card_7239357. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W8661412",
                    "item_ids": ["8426249116", "3453331371"],
                    "new_item_ids": ["9459890810", "7199146548"],
                    "payment_method_id": "credit_card_7239357",
                },
            )
        ],
        outputs=[],
    
        uuid="699bb682-ed7d-4748-a1ad-c6833849460e",),
    
    
    Task(
        annotator="synthetic",
        user_id="yusuf_taylor_7149",
        instruction="Your name is Yusuf Taylor and your zip code is 95154. You are rigid, confident, independent, cautious, direct. For #W2702727, modify Yoga Mat {'thickness': '6mm', 'material': 'natural rubber', 'color': 'pink'} to {'material': 'PVC', 'color': 'green'}; via credit_card_3599838. For #W8268610, change address to {'order_id': '#W8268610', 'address1': '227 Oak Street', 'address2': 'Suite 699', 'city': 'Washington', 'country': 'USA', 'state': 'DC', 'zip': '20564'} (same as #W5690487). For #W8268610, modify Desk Lamp {'color': 'white', 'brightness': 'high', 'power source': 'USB'} to {'color': 'silver', 'brightness': 'low', 'power source': 'AC adapter'}; via credit_card_3599838. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W2702727",
                    "item_ids": ["2733768059"],
                    "new_item_ids": ["7510236436"],
                    "payment_method_id": "credit_card_3599838",
                },
            ),
            Action(
                name="modify_pending_order_address",
                kwargs={
                    "order_id": "#W8268610",
                    "address1": "227 Oak Street",
                    "address2": "Suite 699",
                    "city": "Washington",
                    "country": "USA",
                    "state": "DC",
                    "zip": "20564",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W8268610",
                    "item_ids": ["9083642334"],
                    "new_item_ids": ["1569765161"],
                    "payment_method_id": "credit_card_3599838",
                },
            ),
        ],
        outputs=[],
    
        uuid="7b164517-6205-46ac-97de-f3a4e74c5c2d",),
    Task(
        annotator="synthetic",
        user_id="olivia_ito_3591",
        instruction="Your name is Olivia Ito and your email is olivia.ito5204@example.com. You are dependent, organized, insecure. For #W5442520, modify Patio Umbrella {'size': '7 ft', 'color': 'red', 'material': 'polyester', 'tilt mechanism': 'manual tilt'} to {'size': '6 ft', 'color': 'blue', 'material': 'sunbrella', 'tilt mechanism': 'auto tilt'}; Hiking Boots {'size': '8', 'material': 'leather', 'waterproof': 'yes'} to {'size': '11'}; via gift_card_7794233. For #W3657213, change payment to credit_card_9753331. For #W3657213, modify Digital Camera {'resolution': '24MP', 'zoom': '3x', 'storage': 'SD card'} to {'resolution': '30MP'}; via paypal_8049766. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W5442520",
                    "item_ids": ["3111466194", "2648909398"],
                    "new_item_ids": ["2001307871", "6159919747"],
                    "payment_method_id": "gift_card_7794233",
                },
            ),
            Action(
                name="modify_pending_order_payment",
                kwargs={
                    "order_id": "#W3657213",
                    "payment_method_id": "credit_card_9753331",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W3657213",
                    "item_ids": ["5996159312"],
                    "new_item_ids": ["1804581713"],
                    "payment_method_id": "paypal_8049766",
                },
            ),
        ],
        outputs=[],
    
        uuid="2f82c3e4-8f9d-4ebe-8abb-cd9fe47ee143",),
    Task(
        annotator="synthetic",
        user_id="ethan_sanchez_7289",
        instruction="Your name is Ethan Sanchez and your email is ethan.sanchez3299@example.com. You are optimistic, messy, confident, cautious, impatient. For #W7147989, change address to {'order_id': '#W7147989', 'address1': '386 Cedar Avenue', 'address2': 'Suite 683', 'city': 'Columbus', 'country': 'USA', 'state': 'OH', 'zip': '43119'} (same as #W5560533). For #W7147989, modify Grill {'type': 'electric', 'size': 'portable', 'features': 'none'} to {'features': 'rotisserie'}; Office Chair {'material': 'leather', 'color': 'red', 'armrest': 'none', 'backrest height': 'high-back'} to {'material': 'mesh', 'color': 'gray', 'armrest': 'fixed'}; via gift_card_5917510. ",
        actions=[
            Action(
                name="modify_pending_order_address",
                kwargs={
                    "order_id": "#W7147989",
                    "address1": "386 Cedar Avenue",
                    "address2": "Suite 683",
                    "city": "Columbus",
                    "country": "USA",
                    "state": "OH",
                    "zip": "43119",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W7147989",
                    "item_ids": ["1120917161", "3609437808"],
                    "new_item_ids": ["5745575001", "2386562819"],
                    "payment_method_id": "gift_card_5917510",
                },
            ),
        ],
        outputs=[],
    
        uuid="6e2edd86-9bc7-4233-8de9-cae74e8d64bc",),
    Task(
        annotator="synthetic",
        user_id="mei_martin_4260",
        instruction="Your name is Mei Martin and your zip code is 32124. You are busy, rigid, insecure. For #W7017301, modify Bicycle {'frame size': 'large', 'color': 'red', 'type': 'mountain'} to {'frame size': 'medium', 'color': 'black'}; via paypal_2299608. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W7017301",
                    "item_ids": ["5606522780"],
                    "new_item_ids": ["2143041831"],
                    "payment_method_id": "paypal_2299608",
                },
            )
        ],
        outputs=[],
    
        uuid="f3202f18-3485-4d1e-9afd-2c91a0e9a0f0",),
    Task(
        annotator="synthetic",
        user_id="mei_davis_8935",
        instruction="Your name is Mei Davis and your email is mei.davis6811@example.com. You are busy, cautious, rigid, direct, optimistic. For #W1267569, modify Gaming Mouse {'color': 'white', 'sensor type': 'laser', 'connectivity': 'wireless'} to {'sensor type': 'optical'}; via credit_card_1061405. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W1267569",
                    "item_ids": ["7420906769"],
                    "new_item_ids": ["8896479688"],
                    "payment_method_id": "credit_card_1061405",
                },
            )
        ],
        outputs=[],
    
        uuid="ddb3b810-066b-4142-a82c-bf9ab01f7ff7",),
    Task(
        annotator="synthetic",
        user_id="olivia_jackson_1219",
        instruction="Your name is Olivia Jackson and your email is olivia.jackson2465@example.com. You are logical, dependent, pessimistic, impatient. For #W6975922, modify Jigsaw Puzzle {'pieces': '2000', 'theme': 'animals', 'difficulty level': 'intermediate'} to {'pieces': '1000', 'difficulty level': 'expert'}; via paypal_3999493. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W6975922",
                    "item_ids": ["5645314103"],
                    "new_item_ids": ["4572024853"],
                    "payment_method_id": "paypal_3999493",
                },
            )
        ],
        outputs=[],
    
        uuid="ba7cadff-5398-48b8-9a11-92d790f59857",),
    Task(
        annotator="synthetic",
        user_id="olivia_garcia_4691",
        instruction="Your name is Olivia Garcia and your email is olivia.garcia6676@example.com. You are creative, flexible, shy, sad, polite. For #W3279695, modify Indoor Security Camera {'resolution': '2K', 'field of view': '130 degrees', 'connectivity': 'Ethernet'} to {'resolution': '4K'}; via gift_card_4584785. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W3279695",
                    "item_ids": ["8470360507"],
                    "new_item_ids": ["6901578702"],
                    "payment_method_id": "gift_card_4584785",
                },
            )
        ],
        outputs=[],
    
        uuid="dc9ccca3-e681-4899-9e5d-300d84906cdd",),
    Task(
        annotator="synthetic",
        user_id="noah_ito_3850",
        instruction="Your name is Noah Ito and your email is noah.ito4296@example.com. You are logical, cautious, organized, sad. For #W6729841, change address to {'order_id': '#W6729841', 'address1': '144 Lakeview Drive', 'address2': 'Suite 925', 'city': 'New York', 'country': 'USA', 'state': 'NY', 'zip': '10228'} (same as #W3445693). For #W6729841, modify Bluetooth Speaker {'color': 'black', 'battery life': '10 hours', 'water resistance': 'yes'} to {'color': 'red', 'battery life': '20 hours', 'water resistance': 'no'}; via credit_card_1620755. ",
        actions=[
            Action(
                name="modify_pending_order_address",
                kwargs={
                    "order_id": "#W6729841",
                    "address1": "144 Lakeview Drive",
                    "address2": "Suite 925",
                    "city": "New York",
                    "country": "USA",
                    "state": "NY",
                    "zip": "10228",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W6729841",
                    "item_ids": ["5855700373"],
                    "new_item_ids": ["1052700637"],
                    "payment_method_id": "credit_card_1620755",
                },
            ),
        ],
        outputs=[],
    
        uuid="b440ec81-382a-4f50-b8b2-198d70204b8c",),
    
    Task(
        annotator="synthetic",
        user_id="yusuf_garcia_1670",
        instruction="Your name is Yusuf Garcia and your zip code is 46202. You are sad, dependent. For #W3691773, modify Water Bottle {'capacity': '500ml', 'material': 'stainless steel', 'color': 'green'} to {'capacity': '750ml', 'color': 'red'}; via gift_card_4303603. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W3691773",
                    "item_ids": ["7533802601"],
                    "new_item_ids": ["6777246137"],
                    "payment_method_id": "gift_card_4303603",
                },
            )
        ],
        outputs=[],
    
        uuid="285d5333-ea3b-4e6e-9adc-49fcc490906e",),
    Task(
        annotator="synthetic",
        user_id="lei_patel_5376",
        instruction="Your name is Lei Patel and your email is lei.patel3765@example.com. You are curious, relaxing, insecure. For #W4172216, modify Dumbbell Set {'weight range': '30-50 lbs', 'material': 'rubber', 'set type': 'fixed'} to {'set type': 'adjustable'}; Electric Toothbrush {'color': 'black', 'speed settings': 'high', 'battery type': 'AA batteries'} to {'color': 'white'}; via credit_card_6450011. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W4172216",
                    "item_ids": ["6171242004", "8798690242"],
                    "new_item_ids": ["3735133539", "2645006275"],
                    "payment_method_id": "credit_card_6450011",
                },
            )
        ],
        outputs=[],
    
        uuid="75c5abc5-c166-4474-88f5-7991e50b02ad",),
    Task(
        annotator="synthetic",
        user_id="olivia_ito_3591",
        instruction="Your name is Olivia Ito and your zip code is 80218. You are polite, relaxing, curious, sad. For #W7941031, modify Wristwatch {'strap material': 'leather', 'dial color': 'white'} to {'dial color': 'black'}; via paypal_8049766. For #W3657213, modify Action Camera {'resolution': '4K', 'waterproof': 'yes', 'color': 'black'} to {'resolution': '1080p'}; via gift_card_7794233. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W7941031",
                    "item_ids": ["1355937109"],
                    "new_item_ids": ["9949163720"],
                    "payment_method_id": "paypal_8049766",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W3657213",
                    "item_ids": ["6700049080"],
                    "new_item_ids": ["5925362855"],
                    "payment_method_id": "gift_card_7794233",
                },
            ),
        ],
        outputs=[],
    
        uuid="5d4bbeb2-d86a-45be-9d2e-1373802b4a44",),
    Task(
        annotator="synthetic",
        user_id="harper_kovacs_8617",
        instruction="Your name is Harper Kovacs and your zip code is 95154. You are sad, busy, confident. For #W9093821, modify Wall Clock {'diameter': '10 inches', 'color': 'white', 'type': 'digital'} to {'color': 'black'}; via credit_card_7422485. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W9093821",
                    "item_ids": ["8917609800"],
                    "new_item_ids": ["8610532516"],
                    "payment_method_id": "credit_card_7422485",
                },
            )
        ],
        outputs=[],
    
        uuid="711e49a0-7c1c-4820-8469-e0aa5cf9da76",),
    
    
    Task(
        annotator="synthetic",
        user_id="daiki_silva_5033",
        instruction="Your name is Daiki Silva and your zip code is 28268. You are happy, shy, independent, curious. For #W1579160, modify Tea Kettle {'material': 'glass', 'capacity': '1 liter', 'stovetop compatibility': 'gas'} to {'capacity': '2 liters', 'stovetop compatibility': 'induction'}; Electric Kettle {'capacity': '1.5L', 'material': 'glass', 'color': 'white'} to {'capacity': '2L'}; Vacuum Cleaner {'type': 'upright', 'bagged/bagless': 'bagless', 'features': 'HEPA filter'} to {'type': 'canister', 'bagged/bagless': 'bagged', 'features': 'pet hair removal'}; via paypal_2233507. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W1579160",
                    "item_ids": ["3909406921", "9472539378", "7407609582"],
                    "new_item_ids": ["7292993796", "4064702754", "2872451762"],
                    "payment_method_id": "paypal_2233507",
                },
            )
        ],
        outputs=[],
    
        uuid="095ec21f-677f-4389-980d-6bed932cf766",),
    Task(
        annotator="synthetic",
        user_id="isabella_sanchez_2068",
        instruction="Your name is Isabella Sanchez and your zip code is 85093. You are relaxing, logical, shy. For #W4386313, change address to {'order_id': '#W4386313', 'address1': '964 Sunset Drive', 'address2': 'Suite 782', 'city': 'New York', 'country': 'USA', 'state': 'NY', 'zip': '10199'} (same as #W1713682). For #W4386313, modify Skateboard {'deck material': 'bamboo', 'length': '28 inch', 'design': 'plain'} to {'length': '34 inch', 'design': 'graphic'}; via paypal_8516781. ",
        actions=[
            Action(
                name="modify_pending_order_address",
                kwargs={
                    "order_id": "#W4386313",
                    "address1": "964 Sunset Drive",
                    "address2": "Suite 782",
                    "city": "New York",
                    "country": "USA",
                    "state": "NY",
                    "zip": "10199",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W4386313",
                    "item_ids": ["8176740019"],
                    "new_item_ids": ["3541421151"],
                    "payment_method_id": "paypal_8516781",
                },
            ),
        ],
        outputs=[],
    
        uuid="c9834c89-f023-4f3c-ab98-84c025bfc20c",),
    
    Task(
        annotator="synthetic",
        user_id="mason_lopez_8519",
        instruction="Your name is Mason Lopez and your email is mason.lopez8921@example.com. You are independent, happy, optimistic, messy. For #W9892169, modify Cycling Helmet {'size': 'M', 'color': 'red', 'ventilation': 'low'} to {'size': 'L', 'color': 'black', 'ventilation': 'high'}; via credit_card_2327218. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W9892169",
                    "item_ids": ["6401214406"],
                    "new_item_ids": ["1665571435"],
                    "payment_method_id": "credit_card_2327218",
                },
            )
        ],
        outputs=[],
    
        uuid="6f5755f6-1d59-4035-ba11-31cea7917ae0",),
    Task(
        annotator="synthetic",
        user_id="harper_thomas_9402",
        instruction="Your name is Harper Thomas and your email is harper.thomas1454@example.com. You are pessimistic, creative, messy, shy, dependent. For #W7425646, modify Yoga Mat {'thickness': '6mm', 'material': 'PVC', 'color': 'green'} to {'thickness': '4mm', 'color': 'blue'}; Smart Thermostat {'compatibility': 'Apple HomeKit', 'color': 'black'} to {}; via credit_card_1283450. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W7425646",
                    "item_ids": ["7510236436", "4983901480"],
                    "new_item_ids": ["5586947715", "4983901480"],
                    "payment_method_id": "credit_card_1283450",
                },
            )
        ],
        outputs=[],
    
        uuid="52431559-6b69-4145-a930-71fe09ab57f5",),
    Task(
        annotator="synthetic",
        user_id="ethan_smith_9087",
        instruction="Your name is Ethan Smith and your zip code is 10280. You are messy, polite, shy. For #W6711349, modify Portable Charger {'capacity': '5000mAh', 'output': 'USB-A', 'color': 'white'} to {'capacity': '20000mAh', 'output': 'USB-C'}; Digital Camera {'resolution': '24MP', 'zoom': '5x', 'storage': 'CF card'} to {'resolution': '30MP', 'zoom': '10x', 'storage': 'SD card'}; Electric Toothbrush {'color': 'white', 'speed settings': 'low', 'battery type': 'rechargeable'} to {}; via paypal_3296755. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W6711349",
                    "item_ids": ["7903094618", "4326528037", "6164262152"],
                    "new_item_ids": ["1178356107", "9228757377", "6164262152"],
                    "payment_method_id": "paypal_3296755",
                },
            )
        ],
        outputs=[],
    
        uuid="5152c00d-ccf1-4ed7-a551-7db8347597ca",),
    
    Task(
        annotator="synthetic",
        user_id="yusuf_gonzalez_8900",
        instruction="Your name is Yusuf Gonzalez and your email is yusuf.gonzalez2399@example.com. You are outgoing, sad, flexible, cautious, pessimistic. For #W2806889, change payment to paypal_3022415. For #W2806889, modify Tea Kettle {'material': 'ceramic', 'capacity': '1.5 liters', 'stovetop compatibility': 'gas'} to {'material': 'stainless steel', 'stovetop compatibility': 'induction'}; Smartphone {'color': 'black', 'storage': '128GB', 'RAM': '4GB', 'screen size': '6.5-inch'} to {'RAM': '8GB', 'screen size': '5.8-inch'}; via paypal_3022415. For #W2230795, change payment to credit_card_7918119. For #W2230795, modify Tablet {'screen size': '10-inch', 'storage': '128GB', 'color': 'gold'} to {'storage': '64GB', 'color': 'silver'}; via credit_card_7918119. ",
        actions=[
            Action(
                name="modify_pending_order_payment",
                kwargs={
                    "order_id": "#W2806889",
                    "payment_method_id": "paypal_3022415",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W2806889",
                    "item_ids": ["7497340597", "5339029584"],
                    "new_item_ids": ["3738831434", "1507389580"],
                    "payment_method_id": "paypal_3022415",
                },
            ),
            Action(
                name="modify_pending_order_payment",
                kwargs={
                    "order_id": "#W2230795",
                    "payment_method_id": "credit_card_7918119",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W2230795",
                    "item_ids": ["6948061616"],
                    "new_item_ids": ["2106335193"],
                    "payment_method_id": "credit_card_7918119",
                },
            ),
        ],
        outputs=[],
    
        uuid="c20220a6-b249-4e63-a261-42143dce0c31",),
    Task(
        annotator="synthetic",
        user_id="ivan_hernandez_6923",
        instruction="Your name is Ivan Hernandez and your email is ivan.hernandez1120@example.com. You are flexible, patient, outgoing, messy, insecure. For #W4284542, change address to {'order_id': '#W4284542', 'address1': '894 Hickory Lane', 'address2': 'Suite 665', 'city': 'San Diego', 'country': 'USA', 'state': 'CA', 'zip': '92133'} (same as #W5838674). For #W4284542, change payment to gift_card_9368765. For #W4284542, modify Air Purifier {'room size': 'large', 'filter type': 'HEPA', 'features': 'night mode'} to {'room size': 'medium', 'filter type': 'carbon', 'features': 'quiet operation'}; Bluetooth Speaker {'color': 'red', 'battery life': '10 hours', 'water resistance': 'no'} to {'color': 'blue', 'water resistance': 'yes'}; via gift_card_9368765. ",
        actions=[
            Action(
                name="modify_pending_order_address",
                kwargs={
                    "order_id": "#W4284542",
                    "address1": "894 Hickory Lane",
                    "address2": "Suite 665",
                    "city": "San Diego",
                    "country": "USA",
                    "state": "CA",
                    "zip": "92133",
                },
            ),
            Action(
                name="modify_pending_order_payment",
                kwargs={
                    "order_id": "#W4284542",
                    "payment_method_id": "gift_card_9368765",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W4284542",
                    "item_ids": ["8302289002", "1689914594"],
                    "new_item_ids": ["9375701158", "4716977452"],
                    "payment_method_id": "gift_card_9368765",
                },
            ),
        ],
        outputs=[],
    
        uuid="505d1e42-89e7-4cc0-9a09-816423ddaa9c",),
    Task(
        annotator="synthetic",
        user_id="harper_santos_8115",
        instruction="Your name is Harper Santos and your zip code is 46237. You are direct, independent, happy, messy, busy. For #W4941028, change payment to credit_card_7507679. For #W4941028, modify Backpack {'color': 'grey', 'size': 'large', 'material': 'nylon', 'compartment': 'hydration'} to {'color': 'green', 'size': 'small', 'material': 'polyester', 'compartment': 'laptop'}; Laptop {'screen size': '17-inch', 'processor': 'i9', 'ram': '8GB', 'storage': '256GB SSD', 'color': 'silver'} to {'screen size': '15-inch', 'processor': 'i5', 'ram': '32GB', 'color': 'space grey'}; Smart Thermostat {'compatibility': 'Apple HomeKit', 'color': 'stainless steel'} to {}; via credit_card_7507679. ",
        actions=[
            Action(
                name="modify_pending_order_payment",
                kwargs={
                    "order_id": "#W4941028",
                    "payment_method_id": "credit_card_7507679",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W4941028",
                    "item_ids": ["5726859009", "3265035808", "9480266227"],
                    "new_item_ids": ["3557711149", "2216662955", "9480266227"],
                    "payment_method_id": "credit_card_7507679",
                },
            ),
        ],
        outputs=[],
    
        uuid="aff05e6e-c810-46c4-8bdd-1eb77bf2f410",),
    Task(
        annotator="synthetic",
        user_id="yara_silva_7567",
        instruction="Your name is Yara Silva and your email is yara.silva2443@example.com. You are dependent, confident, optimistic. For #W9810810, modify Bookshelf {'material': 'metal', 'color': 'black', 'height': '6 ft'} to {'material': 'wood', 'color': 'brown', 'height': '5 ft'}; Electric Kettle {'capacity': '1.5L', 'material': 'plastic', 'color': 'white'} to {'color': 'black'}; via gift_card_7252880. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W9810810",
                    "item_ids": ["3778705663", "2698416822"],
                    "new_item_ids": ["2244749153", "5428723833"],
                    "payment_method_id": "gift_card_7252880",
                },
            )
        ],
        outputs=[],
    
        uuid="cd8b671b-abc9-4746-be29-d7c93cec8c12",),
    
    Task(
        annotator="synthetic",
        user_id="emma_kovacs_5477",
        instruction="Your name is Emma Kovacs and your email is emma.kovacs5723@example.com. You are direct, happy, rigid. For #W7109609, modify Headphones {'type': 'on-ear', 'connectivity': 'wireless', 'color': 'white'} to {'type': 'over-ear', 'color': 'black'}; Vacuum Cleaner {'type': 'robotic', 'bagged/bagless': 'bagless', 'features': 'cordless'} to {}; via gift_card_9246707. For #W6554908, modify Perfume {'scent family': 'fresh', 'size': '30ml', 'gender': 'men'} to {'scent family': 'oriental'}; via gift_card_9246707. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W7109609",
                    "item_ids": ["9805150490", "4806644905"],
                    "new_item_ids": ["7493556126", "4806644905"],
                    "payment_method_id": "gift_card_9246707",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W6554908",
                    "item_ids": ["9447903288"],
                    "new_item_ids": ["1325156478"],
                    "payment_method_id": "gift_card_9246707",
                },
            ),
        ],
        outputs=[],
    
        uuid="9979ba50-b9f4-4015-a9bc-8b6490d950e1",),
    Task(
        annotator="synthetic",
        user_id="omar_silva_9907",
        instruction="Your name is Omar Silva and your zip code is 98141. You are polite, happy, shy, dependent, patient. For #W6151519, modify Mechanical Keyboard {'switch type': 'tactile', 'backlight': 'none', 'size': '80%'} to {'switch type': 'clicky'}; Electric Kettle {'capacity': '1L', 'material': 'plastic', 'color': 'silver'} to {'capacity': '2L', 'material': 'glass', 'color': 'white'}; Running Shoes {'size': '9', 'color': 'black', 'material': 'synthetic', 'sole': 'rubber'} to {'color': 'white', 'material': 'mesh'}; via gift_card_5193172. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W6151519",
                    "item_ids": ["7658724607", "9132333852", "4107812777"],
                    "new_item_ids": ["9665000388", "4064702754", "9635758562"],
                    "payment_method_id": "gift_card_5193172",
                },
            )
        ],
        outputs=[],
    
        uuid="a3e5b315-096f-4062-ac20-07158a17a554",),
    
    Task(
        annotator="synthetic",
        user_id="anya_lee_8315",
        instruction="Your name is Anya Lee and your zip code is 78227. You are relaxing, messy, polite, happy. For #W2989580, modify Fleece Jacket {'size': 'L', 'color': 'black', 'zipper': 'full'} to {'size': 'XL', 'color': 'navy'}; via paypal_3728317. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W2989580",
                    "item_ids": ["9385662952"],
                    "new_item_ids": ["7528037711"],
                    "payment_method_id": "paypal_3728317",
                },
            )
        ],
        outputs=[],
    
        uuid="2e877ad4-be18-4c9f-ad81-55e7e247f2bd",),
    Task(
        annotator="synthetic",
        user_id="emma_kovacs_9839",
        instruction="Your name is Emma Kovacs and your email is emma.kovacs2974@example.com. You are pessimistic, impatient, sad, flexible, outgoing. For #W8661412, modify Water Bottle {'capacity': '500ml', 'material': 'stainless steel', 'color': 'black'} to {'capacity': '750ml', 'color': 'red'}; via credit_card_7239357. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W8661412",
                    "item_ids": ["3453331371"],
                    "new_item_ids": ["6777246137"],
                    "payment_method_id": "credit_card_7239357",
                },
            )
        ],
        outputs=[],
    
        uuid="ab798ffa-ed72-4adc-ab37-8ab840d6ef91",),
    Task(
        annotator="synthetic",
        user_id="fatima_anderson_2157",
        instruction="Your name is Fatima Anderson and your zip code is 32100. You are impatient, organized. For #W2974929, modify Skateboard {'deck material': 'plastic', 'length': '31 inch', 'design': 'plain'} to {'length': '34 inch', 'design': 'graphic'}; via paypal_7916550. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W2974929",
                    "item_ids": ["3877188862"],
                    "new_item_ids": ["5489028872"],
                    "payment_method_id": "paypal_7916550",
                },
            )
        ],
        outputs=[],
    
        uuid="4cbba1d6-f7e9-41f6-bc4e-d16ae37416a6",),
    Task(
        annotator="synthetic",
        user_id="daiki_muller_8062",
        instruction="Your name is Daiki Muller and your zip code is 94157. You are patient, sad. For #W6790887, modify Dumbbell Set {'weight range': '5-25 lbs', 'material': 'urethane', 'set type': 'fixed'} to {'weight range': '30-50 lbs', 'set type': 'adjustable'}; via gift_card_8385925. For #W7822344, modify Electric Kettle {'capacity': '1L', 'material': 'stainless steel', 'color': 'silver'} to {'color': 'black'}; via gift_card_8385925. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W6790887",
                    "item_ids": ["6585768447"],
                    "new_item_ids": ["4422467033"],
                    "payment_method_id": "gift_card_8385925",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W7822344",
                    "item_ids": ["8142779083"],
                    "new_item_ids": ["7602931732"],
                    "payment_method_id": "gift_card_8385925",
                },
            ),
        ],
        outputs=[],
    
        uuid="afec6b5b-4b5a-4509-aae6-2ba0dec77442",),
    Task(
        annotator="synthetic",
        user_id="sophia_garcia_5795",
        instruction="Your name is Sophia Garcia and your zip code is 28212. You are organized, curious, impatient. For #W4958652, change address to {'order_id': '#W4958652', 'address1': '536 Cedar Street', 'address2': 'Suite 916', 'city': 'Charlotte', 'country': 'USA', 'state': 'NC', 'zip': '28212'} (same as #W6447372). For #W4958652, modify Cycling Helmet {'size': 'L', 'color': 'black', 'ventilation': 'high'} to {'size': 'S', 'color': 'blue', 'ventilation': 'low'}; Tea Kettle {'material': 'stainless steel', 'capacity': '2 liters', 'stovetop compatibility': 'induction'} to {'material': 'glass'}; Office Chair {'material': 'fabric', 'color': 'blue', 'armrest': 'adjustable', 'backrest height': 'standard'} to {'material': 'mesh', 'color': 'red', 'armrest': 'none'}; Smart Thermostat {'compatibility': 'Google Assistant', 'color': 'stainless steel'} to {'compatibility': 'Apple HomeKit', 'color': 'black'}; via credit_card_9467292. ",
        actions=[
            Action(
                name="modify_pending_order_address",
                kwargs={
                    "order_id": "#W4958652",
                    "address1": "536 Cedar Street",
                    "address2": "Suite 916",
                    "city": "Charlotte",
                    "country": "USA",
                    "state": "NC",
                    "zip": "28212",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W4958652",
                    "item_ids": [
                        "1665571435",
                        "1906487464",
                        "8323284863",
                        "2791467853",
                    ],
                    "new_item_ids": [
                        "5886093635",
                        "7292993796",
                        "4274709903",
                        "4983901480",
                    ],
                    "payment_method_id": "credit_card_9467292",
                },
            ),
        ],
        outputs=[],
    
        uuid="d61644ca-94a9-41cf-81d4-42cce1762306",),
    Task(
        annotator="synthetic",
        user_id="ava_nguyen_4072",
        instruction="Your name is Ava Nguyen and your email is ava.nguyen1851@example.com. You are relaxing, curious. For #W2601346, modify Makeup Kit {'skin tone': 'medium', 'kit size': 'professional', 'brand': 'Brand C'} to {'skin tone': 'dark', 'brand': 'Brand A'}; via paypal_3180577. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W2601346",
                    "item_ids": ["7736359414"],
                    "new_item_ids": ["1573035764"],
                    "payment_method_id": "paypal_3180577",
                },
            )
        ],
        outputs=[],
    
        uuid="92df7124-ec4f-41ac-b1d3-99df8715f510",),
    Task(
        annotator="synthetic",
        user_id="olivia_ito_3591",
        instruction="Your name is Olivia Ito and your zip code is 80218. You are logical, curious. For #W7941031, change payment to paypal_8049766. For #W7941031, modify Backpack {'color': 'grey', 'size': 'medium', 'material': 'polyester', 'compartment': 'laptop'} to {'size': 'small', 'material': 'nylon'}; via gift_card_7794233. ",
        actions=[
            Action(
                name="modify_pending_order_payment",
                kwargs={
                    "order_id": "#W7941031",
                    "payment_method_id": "paypal_8049766",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W7941031",
                    "item_ids": ["5917587651"],
                    "new_item_ids": ["8054888773"],
                    "payment_method_id": "gift_card_7794233",
                },
            ),
        ],
        outputs=[],
    
        uuid="73ef2a6c-b1f8-4390-969a-f0513b53a2dc",),
    Task(
        annotator="synthetic",
        user_id="sofia_davis_2103",
        instruction="Your name is Sofia Davis and your zip code is 98151. You are pessimistic, insecure, messy, direct, curious. For #W2541482, modify Espresso Machine {'pressure': '15 bar', 'capacity': '1L', 'type': 'manual'} to {'pressure': '9 bar', 'capacity': '1.5L', 'type': 'capsule'}; Tea Kettle {'material': 'ceramic', 'capacity': '1.5 liters', 'stovetop compatibility': 'gas'} to {'material': 'glass', 'capacity': '2 liters', 'stovetop compatibility': 'electric'}; via gift_card_3377580. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W2541482",
                    "item_ids": ["3714494375", "7497340597"],
                    "new_item_ids": ["3815173328", "2820119811"],
                    "payment_method_id": "gift_card_3377580",
                },
            )
        ],
        outputs=[],
    
        uuid="9fcff612-05d8-4127-b339-4ae7223e2276",),
    Task(
        annotator="synthetic",
        user_id="ethan_moore_3587",
        instruction="Your name is Ethan Moore and your email is ethan.moore4935@example.com. You are happy, insecure. For #W7584328, modify Backpack {'color': 'navy', 'size': 'small', 'material': 'nylon', 'compartment': 'laptop'} to {'color': 'black', 'size': 'large', 'material': 'polyester'}; via credit_card_6173085. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W7584328",
                    "item_ids": ["2492465580"],
                    "new_item_ids": ["6906307980"],
                    "payment_method_id": "credit_card_6173085",
                },
            )
        ],
        outputs=[],
    
        uuid="3622b9bf-3516-4978-abf8-d7e02a951aa4",),
    Task(
        annotator="synthetic",
        user_id="yusuf_hernandez_6785",
        instruction="Your name is Yusuf Hernandez and your zip code is 80265. You are confident, flexible. For #W6832752, change address to {'order_id': '#W6832752', 'address1': '580 Broadway', 'address2': 'Suite 162', 'city': 'Denver', 'country': 'USA', 'state': 'CO', 'zip': '80265'} (same as #W2166301). For #W6832752, modify Hiking Boots {'size': '7', 'material': 'leather', 'waterproof': 'yes'} to {'material': 'synthetic', 'waterproof': 'no'}; via paypal_7529813. For #W2166301, modify Running Shoes {'size': '10', 'color': 'white', 'material': 'leather', 'sole': 'EVA'} to {'size': '8', 'color': 'red'}; via paypal_7529813. ",
        actions=[
            Action(
                name="modify_pending_order_address",
                kwargs={
                    "order_id": "#W6832752",
                    "address1": "580 Broadway",
                    "address2": "Suite 162",
                    "city": "Denver",
                    "country": "USA",
                    "state": "CO",
                    "zip": "80265",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W6832752",
                    "item_ids": ["3812493782"],
                    "new_item_ids": ["1437889264"],
                    "payment_method_id": "paypal_7529813",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W2166301",
                    "item_ids": ["1775591963"],
                    "new_item_ids": ["4153505238"],
                    "payment_method_id": "paypal_7529813",
                },
            ),
        ],
        outputs=[],
    
        uuid="e331b2ee-7e02-47c6-981f-d617ea9c4978",),
    Task(
        annotator="synthetic",
        user_id="fatima_anderson_7445",
        instruction="Your name is Fatima Anderson and your zip code is 78786. You are pessimistic, rigid, sad, shy, messy. For #W6368178, change payment to gift_card_8070316. For #W6368178, modify Electric Kettle {'capacity': '1L', 'material': 'plastic', 'color': 'white'} to {'capacity': '1.5L'}; via gift_card_8070316. ",
        actions=[
            Action(
                name="modify_pending_order_payment",
                kwargs={
                    "order_id": "#W6368178",
                    "payment_method_id": "gift_card_8070316",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W6368178",
                    "item_ids": ["2243454707"],
                    "new_item_ids": ["2698416822"],
                    "payment_method_id": "gift_card_8070316",
                },
            ),
        ],
        outputs=[],
    
        uuid="4b21fc63-5f83-44cf-92a5-b526bdbca2bf",),
    Task(
        annotator="synthetic",
        user_id="chen_johnson_4204",
        instruction="Your name is Chen Johnson and your email is chen.johnson3889@example.com. You are pessimistic, polite, patient, organized, creative. For #W5061109, modify Bluetooth Speaker {'color': 'blue', 'battery life': '20 hours', 'water resistance': 'yes'} to {'color': 'green', 'water resistance': 'no'}; via paypal_3742148. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W5061109",
                    "item_ids": ["3254583681"],
                    "new_item_ids": ["9440686670"],
                    "payment_method_id": "paypal_3742148",
                },
            )
        ],
        outputs=[],
    
        uuid="9fb5fc67-af19-4ab8-9ae8-4c7f267d69ce",),
    Task(
        annotator="synthetic",
        user_id="lei_li_6575",
        instruction="Your name is Lei Li and your zip code is 85033. You are outgoing, rigid. For #W3414433, modify Digital Camera {'resolution': '30MP', 'zoom': '3x', 'storage': 'SD card'} to {'resolution': '20MP'}; Electric Kettle {'capacity': '1L', 'material': 'stainless steel', 'color': 'black'} to {'material': 'glass'}; via gift_card_8049813. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W3414433",
                    "item_ids": ["1804581713", "7602931732"],
                    "new_item_ids": ["8363011723", "2323972008"],
                    "payment_method_id": "gift_card_8049813",
                },
            )
        ],
        outputs=[],
    
        uuid="8452ea95-05ed-48ec-bcf9-7454b5448a88",),
    Task(
        annotator="synthetic",
        user_id="emma_kovacs_7176",
        instruction="Your name is Emma Kovacs and your zip code is 32254. You are happy, rigid, creative, polite. For #W2307204, modify Notebook {'size': 'A6', 'cover type': 'soft cover'} to {'size': 'A4', 'cover type': 'hard cover'}; via paypal_1038468. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W2307204",
                    "item_ids": ["9421195098"],
                    "new_item_ids": ["1199058591"],
                    "payment_method_id": "paypal_1038468",
                },
            )
        ],
        outputs=[],
    
        uuid="58ee0f84-1735-43a3-9de4-4d91bc182c6e",),
    Task(
        annotator="synthetic",
        user_id="chen_johnson_4204",
        instruction="Your name is Chen Johnson and your email is chen.johnson3889@example.com. You are patient, happy, messy, independent, cautious. For #W5061109, modify Bluetooth Speaker {'color': 'blue', 'battery life': '20 hours', 'water resistance': 'yes'} to {}; Office Chair {'material': 'fabric', 'color': 'blue', 'armrest': 'adjustable', 'backrest height': 'standard'} to {'color': 'black', 'armrest': 'fixed'}; Makeup Kit {'skin tone': 'dark', 'kit size': 'basic', 'brand': 'Brand B'} to {'kit size': 'professional'}; via paypal_3742148. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W5061109",
                    "item_ids": ["3254583681", "8323284863", "6254646215"],
                    "new_item_ids": ["3254583681", "8426249116", "5012998807"],
                    "payment_method_id": "paypal_3742148",
                },
            )
        ],
        outputs=[],
    
        uuid="16bad91f-2527-4ef2-a6ce-9f0d6944f8be",),
    Task(
        annotator="synthetic",
        user_id="yusuf_ahmed_6232",
        instruction="Your name is Yusuf Ahmed and your email is yusuf.ahmed5476@example.com. You are messy, confident, busy, direct. For #W7007896, modify Laptop {'screen size': '13-inch', 'processor': 'i9', 'ram': '8GB', 'storage': '1TB SSD', 'color': 'space grey'} to {'processor': 'i5', 'ram': '16GB', 'storage': '512GB SSD'}; via credit_card_2167533. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W7007896",
                    "item_ids": ["8193934556"],
                    "new_item_ids": ["6056040996"],
                    "payment_method_id": "credit_card_2167533",
                },
            )
        ],
        outputs=[],
    
        uuid="b1a4ff7d-017b-4b78-b3ea-97f3adeaa838",),
    Task(
        annotator="synthetic",
        user_id="james_kim_7213",
        instruction="Your name is James Kim and your zip code is 92199. You are curious, patient, shy, dependent, organized. For #W9722559, change address to {'order_id': '#W9722559', 'address1': '320 Cedar Avenue', 'address2': 'Suite 116', 'city': 'San Antonio', 'country': 'USA', 'state': 'TX', 'zip': '78219'} (same as #W9154975). For #W9722559, modify Luggage Set {'piece count': '2-piece', 'color': 'red', 'material': 'hardshell'} to {'piece count': '3-piece', 'color': 'blue', 'material': 'softshell'}; via paypal_8963303. ",
        actions=[
            Action(
                name="modify_pending_order_address",
                kwargs={
                    "order_id": "#W9722559",
                    "address1": "320 Cedar Avenue",
                    "address2": "Suite 116",
                    "city": "San Antonio",
                    "country": "USA",
                    "state": "TX",
                    "zip": "78219",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W9722559",
                    "item_ids": ["8964750292"],
                    "new_item_ids": ["6301799585"],
                    "payment_method_id": "paypal_8963303",
                },
            ),
        ],
        outputs=[],
    
        uuid="81c87700-fe67-4aa7-93cf-de0043c22723",),
    
    Task(
        annotator="synthetic",
        user_id="mei_gonzalez_4785",
        instruction="Your name is Mei Gonzalez and your zip code is 95170. You are patient, busy, polite. For #W2052757, modify Notebook {'size': 'A5', 'cover type': 'soft cover'} to {'size': 'A4'}; Office Chair {'material': 'mesh', 'color': 'red', 'armrest': 'none', 'backrest height': 'standard'} to {'color': 'gray', 'armrest': 'fixed', 'backrest height': 'high-back'}; via credit_card_4387170. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W2052757",
                    "item_ids": ["9799386954", "4274709903"],
                    "new_item_ids": ["7579176349", "2386562819"],
                    "payment_method_id": "credit_card_4387170",
                },
            )
        ],
        outputs=[],
    
        uuid="ce98580b-8afb-42e4-ab32-f5c156075f45",),
    Task(
        annotator="synthetic",
        user_id="daiki_khan_6856",
        instruction="Your name is Daiki Khan and your email is daiki.khan2146@example.com. You are shy, sad, dependent, confident, organized. For #W8461477, modify Action Camera {'resolution': '1080p', 'waterproof': 'no', 'color': 'silver'} to {'resolution': '4K', 'waterproof': 'yes'}; via gift_card_2491643. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W8461477",
                    "item_ids": ["1810466394"],
                    "new_item_ids": ["6117189161"],
                    "payment_method_id": "gift_card_2491643",
                },
            )
        ],
        outputs=[],
    
        uuid="b82bae69-8156-4485-8eda-301ee57beb66",),
    
    Task(
        annotator="synthetic",
        user_id="sofia_li_8235",
        instruction="Your name is Sofia Li and your zip code is 75390. You are flexible, organized, relaxing. For #W6599568, change payment to credit_card_8296913. For #W6599568, modify Bluetooth Speaker {'color': 'red', 'battery life': '20 hours', 'water resistance': 'no'} to {'color': 'blue'}; via credit_card_8296913. ",
        actions=[
            Action(
                name="modify_pending_order_payment",
                kwargs={
                    "order_id": "#W6599568",
                    "payment_method_id": "credit_card_8296913",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W6599568",
                    "item_ids": ["1052700637"],
                    "new_item_ids": ["2635605237"],
                    "payment_method_id": "credit_card_8296913",
                },
            ),
        ],
        outputs=[],
    
        uuid="42f4076e-9845-4898-b7e7-4a407d4f6ceb",),
    Task(
        annotator="synthetic",
        user_id="olivia_silva_7273",
        instruction="Your name is Olivia Silva and your zip code is 32240. You are patient, flexible, organized, optimistic, cautious. For #W7613749, modify Wall Clock {'diameter': '12 inches', 'color': 'white', 'type': 'analog'} to {'diameter': '10 inches', 'color': 'wood'}; Smartphone {'color': 'rose gold', 'storage': '64GB', 'RAM': '8GB', 'screen size': '5.8-inch'} to {'color': 'black', 'storage': '128GB'}; via paypal_9379149. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W7613749",
                    "item_ids": ["6508153405", "5311660992"],
                    "new_item_ids": ["6534134392", "1507389580"],
                    "payment_method_id": "paypal_9379149",
                },
            )
        ],
        outputs=[],
    
        uuid="f1619f35-129d-4aed-a61f-6872f4ba09a1",),
    
    Task(
        annotator="synthetic",
        user_id="yara_lee_7701",
        instruction="Your name is Yara Lee and your zip code is 77243. You are pessimistic, insecure, rigid, outgoing, direct. For #W3320020, modify Office Chair {'material': 'leather', 'color': 'red', 'armrest': 'none', 'backrest height': 'high-back'} to {'material': 'mesh', 'color': 'blue', 'armrest': 'fixed', 'backrest height': 'standard'}; via credit_card_6680679. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W3320020",
                    "item_ids": ["3609437808"],
                    "new_item_ids": ["3704016729"],
                    "payment_method_id": "credit_card_6680679",
                },
            )
        ],
        outputs=[],
    
        uuid="edef725d-4c53-4b72-8344-66b3314577ae",),
    Task(
        annotator="synthetic",
        user_id="omar_silva_7446",
        instruction="Your name is Omar Silva and your email is omar.silva4147@example.com. You are confident, logical, happy. For #W9673784, modify Espresso Machine {'pressure': '19 bar', 'capacity': '1L', 'type': 'manual'} to {'pressure': '15 bar'}; via paypal_2192303. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W9673784",
                    "item_ids": ["9884666842"],
                    "new_item_ids": ["3714494375"],
                    "payment_method_id": "paypal_2192303",
                },
            )
        ],
        outputs=[],
    
        uuid="877d3066-46b7-475b-a15a-2b023aa77f03",),
    
    Task(
        annotator="synthetic",
        user_id="aarav_davis_4756",
        instruction="Your name is Aarav Davis and your email is aarav.davis1165@example.com. You are organized, patient, independent, logical. For #W3196599, change address to {'order_id': '#W3196599', 'address1': '178 Lakeview Drive', 'address2': 'Suite 576', 'city': 'Fort Worth', 'country': 'USA', 'state': 'TX', 'zip': '76150'} (same as #W7430166). For #W3196599, modify Dumbbell Set {'weight range': '30-50 lbs', 'material': 'rubber', 'set type': 'fixed'} to {'weight range': '55-75 lbs', 'material': 'iron'}; via gift_card_9708163. For #W7430166, change address to {'order_id': '#W7430166', 'address1': '808 Chestnut Street', 'address2': 'Suite 832', 'city': 'Phoenix', 'country': 'USA', 'state': 'AZ', 'zip': '85072'} (same as #W2403075). For #W7430166, modify Electric Kettle {'capacity': '1L', 'material': 'glass', 'color': 'silver'} to {'color': 'white'}; via gift_card_9708163. ",
        actions=[
            Action(
                name="modify_pending_order_address",
                kwargs={
                    "order_id": "#W3196599",
                    "address1": "178 Lakeview Drive",
                    "address2": "Suite 576",
                    "city": "Fort Worth",
                    "country": "USA",
                    "state": "TX",
                    "zip": "76150",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W3196599",
                    "item_ids": ["6171242004"],
                    "new_item_ids": ["2444431651"],
                    "payment_method_id": "gift_card_9708163",
                },
            ),
            Action(
                name="modify_pending_order_address",
                kwargs={
                    "order_id": "#W7430166",
                    "address1": "808 Chestnut Street",
                    "address2": "Suite 832",
                    "city": "Phoenix",
                    "country": "USA",
                    "state": "AZ",
                    "zip": "85072",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W7430166",
                    "item_ids": ["1240311797"],
                    "new_item_ids": ["5268233322"],
                    "payment_method_id": "gift_card_9708163",
                },
            ),
        ],
        outputs=[],
    
        uuid="f1b24181-57d5-4125-813c-dd3878c46c1e",),
    Task(
        annotator="synthetic",
        user_id="james_lee_5010",
        instruction="Your name is James Lee and your zip code is 95161. You are busy, polite, cautious, impatient, insecure. For #W5356919, modify Jigsaw Puzzle {'pieces': '1000', 'theme': 'art', 'difficulty level': 'expert'} to {'pieces': '500', 'difficulty level': 'intermediate'}; via paypal_2684483. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W5356919",
                    "item_ids": ["9370300555"],
                    "new_item_ids": ["4068787148"],
                    "payment_method_id": "paypal_2684483",
                },
            )
        ],
        outputs=[],
    
        uuid="d817cea6-3fe2-4e87-ac9d-42546e27f8c4",),
    
    Task(
        annotator="synthetic",
        user_id="liam_santos_5468",
        instruction="Your name is Liam Santos and your zip code is 78762. You are polite, organized. For #W6794581, change address to {'order_id': '#W6794581', 'address1': '441 Hillcrest Drive', 'address2': 'Suite 386', 'city': 'Austin', 'country': 'USA', 'state': 'TX', 'zip': '78762'} (same as #W4011814). For #W6794581, modify Tea Kettle {'material': 'stainless steel', 'capacity': '2 liters', 'stovetop compatibility': 'induction'} to {'material': 'glass', 'capacity': '1 liter', 'stovetop compatibility': 'gas'}; Cycling Helmet {'size': 'M', 'color': 'red', 'ventilation': 'medium'} to {'size': 'L', 'color': 'black', 'ventilation': 'high'}; via credit_card_1055108. ",
        actions=[
            Action(
                name="modify_pending_order_address",
                kwargs={
                    "order_id": "#W6794581",
                    "address1": "441 Hillcrest Drive",
                    "address2": "Suite 386",
                    "city": "Austin",
                    "country": "USA",
                    "state": "TX",
                    "zip": "78762",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W6794581",
                    "item_ids": ["1906487464", "1719127154"],
                    "new_item_ids": ["3909406921", "1665571435"],
                    "payment_method_id": "credit_card_1055108",
                },
            ),
        ],
        outputs=[],
    
        uuid="106603d8-bdc0-4d06-bd80-1e973c37c24f",),
    Task(
        annotator="synthetic",
        user_id="omar_kim_3528",
        instruction="Your name is Omar Kim and your zip code is 32214. You are busy, happy, optimistic. For #W7111824, modify Espresso Machine {'pressure': '19 bar', 'capacity': '1L', 'type': 'manual'} to {'pressure': '9 bar', 'capacity': '2L'}; via credit_card_3577130. For #W1080318, change payment to gift_card_3749819. For #W1080318, modify T-Shirt {'color': 'blue', 'size': 'S', 'material': 'cotton', 'style': 'v-neck'} to {'color': 'black', 'size': 'XL', 'style': 'crew neck'}; via gift_card_3749819. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W7111824",
                    "item_ids": ["9884666842"],
                    "new_item_ids": ["7774234341"],
                    "payment_method_id": "credit_card_3577130",
                },
            ),
            Action(
                name="modify_pending_order_payment",
                kwargs={
                    "order_id": "#W1080318",
                    "payment_method_id": "gift_card_3749819",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W1080318",
                    "item_ids": ["8349118980"],
                    "new_item_ids": ["2060066974"],
                    "payment_method_id": "gift_card_3749819",
                },
            ),
        ],
        outputs=[],
    
        uuid="3c9769ab-9d68-4cf6-9787-bf533ddb6173",),
    
    
    Task(
        annotator="synthetic",
        user_id="olivia_lopez_9494",
        instruction="Your name is Olivia Lopez and your email is olivia.lopez8783@example.com. You are cautious, organized, creative, impatient, busy. For #W8955613, change payment to credit_card_6044108. For #W8955613, modify Backpack {'color': 'grey', 'size': 'large', 'material': 'polyester', 'compartment': 'hydration'} to {'color': 'green', 'size': 'small', 'compartment': 'camera'}; Smart Watch {'color': 'gold', 'band material': 'metal', 'display': 'AMOLED'} to {'band material': 'silicone'}; via credit_card_6044108. ",
        actions=[
            Action(
                name="modify_pending_order_payment",
                kwargs={
                    "order_id": "#W8955613",
                    "payment_method_id": "credit_card_6044108",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W8955613",
                    "item_ids": ["6309044598", "2554056026"],
                    "new_item_ids": ["9851293632", "2681513500"],
                    "payment_method_id": "credit_card_6044108",
                },
            ),
        ],
        outputs=[],
    
        uuid="1865bb16-966d-4d91-bd6f-6cf213b2bdce",),
    Task(
        annotator="synthetic",
        user_id="yusuf_khan_7091",
        instruction="Your name is Yusuf Khan and your zip code is 75313. You are dependent, patient. For #W3579467, modify Electric Kettle {'capacity': '1.5L', 'material': 'plastic', 'color': 'black'} to {'color': 'white'}; via paypal_5796936. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W3579467",
                    "item_ids": ["5428723833"],
                    "new_item_ids": ["2698416822"],
                    "payment_method_id": "paypal_5796936",
                },
            )
        ],
        outputs=[],
    
        uuid="8ad8f65e-90c9-49e8-ab29-239efcfd6391",),
    Task(
        annotator="synthetic",
        user_id="olivia_smith_5265",
        instruction="Your name is Olivia Smith and your zip code is 80216. You are curious, confident. For #W1974181, modify Wristwatch {'strap material': 'silicone', 'dial color': 'blue'} to {}; via credit_card_7971769. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W1974181",
                    "item_ids": ["8886009523"],
                    "new_item_ids": ["8886009523"],
                    "payment_method_id": "credit_card_7971769",
                },
            )
        ],
        outputs=[],
    
        uuid="ea42b85a-075a-4b3d-9d5e-2598bc7d7a3a",),
    Task(
        annotator="synthetic",
        user_id="daiki_silva_2903",
        instruction="Your name is Daiki Silva and your email is daiki.silva6295@example.com. You are pessimistic, insecure, creative, dependent, outgoing. For #W8835847, modify Bookshelf {'material': 'glass', 'color': 'white', 'height': '5 ft'} to {'material': 'wood', 'height': '4 ft'}; T-Shirt {'color': 'red', 'size': 'XXL', 'material': 'cotton', 'style': 'crew neck'} to {'color': 'blue', 'size': 'S', 'style': 'v-neck'}; Gaming Mouse {'color': 'white', 'sensor type': 'laser', 'connectivity': 'wireless'} to {'color': 'black'}; via gift_card_2652153. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W8835847",
                    "item_ids": ["8895454203", "9354168549", "7420906769"],
                    "new_item_ids": ["8920458606", "8349118980", "8214883393"],
                    "payment_method_id": "gift_card_2652153",
                },
            )
        ],
        outputs=[],
    
        uuid="070b834a-ecce-459c-a8c4-9c6478405dde",),
    Task(
        annotator="synthetic",
        user_id="ethan_smith_9087",
        instruction="Your name is Ethan Smith and your zip code is 10280. You are flexible, dependent, sad, patient, insecure. For #W6711349, modify Digital Camera {'resolution': '24MP', 'zoom': '5x', 'storage': 'CF card'} to {'resolution': '30MP', 'zoom': '3x', 'storage': 'SD card'}; Electric Toothbrush {'color': 'white', 'speed settings': 'low', 'battery type': 'rechargeable'} to {'color': 'blue', 'battery type': 'AA batteries'}; via paypal_3296755. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W6711349",
                    "item_ids": ["4326528037", "6164262152"],
                    "new_item_ids": ["1804581713", "1583904702"],
                    "payment_method_id": "paypal_3296755",
                },
            )
        ],
        outputs=[],
    
        uuid="11a60ce6-a7f5-47c0-aa41-7284dcfba916",),
    Task(
        annotator="synthetic",
        user_id="sophia_nguyen_7885",
        instruction="Your name is Sophia Nguyen and your zip code is 60647. You are shy, optimistic, organized, logical, flexible. For #W4183735, modify Smartphone {'color': 'rose gold', 'storage': '64GB', 'RAM': '8GB', 'screen size': '5.8-inch'} to {'color': 'black', 'storage': '128GB', 'RAM': '4GB', 'screen size': '6.5-inch'}; via gift_card_2415038. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W4183735",
                    "item_ids": ["5311660992"],
                    "new_item_ids": ["5339029584"],
                    "payment_method_id": "gift_card_2415038",
                },
            )
        ],
        outputs=[],
    
        uuid="4fd84b4a-cab2-465b-9a48-81844b3d344e",),
    Task(
        annotator="synthetic",
        user_id="aarav_gonzalez_5113",
        instruction="Your name is Aarav Gonzalez and your zip code is 78268. You are direct, organized, patient. For #W6979932, change payment to gift_card_5979071. For #W6979932, change address to {'order_id': '#W6979932', 'address1': '270 River Road', 'address2': 'Suite 611', 'city': 'San Diego', 'country': 'USA', 'state': 'CA', 'zip': '92194'} (same as #W6797115). For #W6979932, modify Cycling Helmet {'size': 'M', 'color': 'blue', 'ventilation': 'low'} to {'size': 'L', 'color': 'black', 'ventilation': 'high'}; via paypal_6121064. ",
        actions=[
            Action(
                name="modify_pending_order_payment",
                kwargs={
                    "order_id": "#W6979932",
                    "payment_method_id": "gift_card_5979071",
                },
            ),
            Action(
                name="modify_pending_order_address",
                kwargs={
                    "order_id": "#W6979932",
                    "address1": "270 River Road",
                    "address2": "Suite 611",
                    "city": "San Diego",
                    "country": "USA",
                    "state": "CA",
                    "zip": "92194",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W6979932",
                    "item_ids": ["3339188619"],
                    "new_item_ids": ["1665571435"],
                    "payment_method_id": "paypal_6121064",
                },
            ),
        ],
        outputs=[],
    
        uuid="c31617ae-85c6-4a6e-a75b-7eecadd07dd9",),
    
    Task(
        annotator="synthetic",
        user_id="yusuf_ahmed_6232",
        instruction="Your name is Yusuf Ahmed and your zip code is 91075. You are patient, optimistic, creative. For #W1302858, modify Gaming Mouse {'color': 'RGB', 'sensor type': 'optical', 'connectivity': 'wired'} to {'color': 'white', 'connectivity': 'wireless'}; via credit_card_2167533. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W1302858",
                    "item_ids": ["5796612084"],
                    "new_item_ids": ["8896479688"],
                    "payment_method_id": "credit_card_2167533",
                },
            )
        ],
        outputs=[],
    
        uuid="3b76e6a4-8670-4603-a7f3-481d00f9851c",),
    Task(
        annotator="synthetic",
        user_id="amelia_rossi_5121",
        instruction="Your name is Amelia Rossi and your email is amelia.rossi1299@example.com. You are flexible, direct. For #W8255453, modify Laptop {'screen size': '17-inch', 'processor': 'i5', 'ram': '8GB', 'storage': '1TB SSD', 'color': 'space grey'} to {'screen size': '13-inch', 'ram': '16GB', 'storage': '512GB SSD'}; T-Shirt {'color': 'blue', 'size': 'M', 'material': 'cotton', 'style': 'crew neck'} to {'color': 'black', 'size': 'XXL', 'material': 'polyester', 'style': 'v-neck'}; via gift_card_5591026. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W8255453",
                    "item_ids": ["3334537816", "9612497925"],
                    "new_item_ids": ["6056040996", "5253880258"],
                    "payment_method_id": "gift_card_5591026",
                },
            )
        ],
        outputs=[],
    
        uuid="497f87f1-db20-4f4a-b107-5f38cce611a8",),
    Task(
        annotator="synthetic",
        user_id="lei_patel_5376",
        instruction="Your name is Lei Patel and your email is lei.patel3765@example.com. You are optimistic, messy, relaxing, creative, shy. For #W4172216, modify Skateboard {'deck material': 'maple', 'length': '34 inch', 'design': 'graphic'} to {'deck material': 'bamboo', 'length': '31 inch', 'design': 'custom'}; Electric Toothbrush {'color': 'black', 'speed settings': 'high', 'battery type': 'AA batteries'} to {'color': 'white', 'speed settings': 'low', 'battery type': 'rechargeable'}; via credit_card_6450011. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W4172216",
                    "item_ids": ["2343503231", "8798690242"],
                    "new_item_ids": ["6313971174", "6164262152"],
                    "payment_method_id": "credit_card_6450011",
                },
            )
        ],
        outputs=[],
    
        uuid="bc47657e-fbef-42f7-94f5-54ab1f64ea4f",),
    Task(
        annotator="synthetic",
        user_id="emma_martin_6993",
        instruction="Your name is Emma Martin and your zip code is 78750. You are organized, insecure, shy, creative. For #W5432440, modify Portable Charger {'capacity': '20000mAh', 'output': 'Wireless', 'color': 'black'} to {'capacity': '10000mAh', 'output': 'USB-C', 'color': 'blue'}; via paypal_6129397. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W5432440",
                    "item_ids": ["8349903180"],
                    "new_item_ids": ["7884173033"],
                    "payment_method_id": "paypal_6129397",
                },
            )
        ],
        outputs=[],
    
        uuid="20d88060-fc21-437d-b8a4-fb3e82554e33",),
    Task(
        annotator="synthetic",
        user_id="liam_thomas_8833",
        instruction="Your name is Liam Thomas and your email is liam.thomas4271@example.com. You are direct, relaxing, pessimistic. For #W3761872, modify Vacuum Cleaner {'type': 'upright', 'bagged/bagless': 'bagless', 'features': 'cordless'} to {'type': 'canister', 'features': 'pet hair removal'}; Espresso Machine {'pressure': '9 bar', 'capacity': '2L', 'type': 'automatic'} to {'capacity': '1L', 'type': 'capsule'}; via credit_card_7287775. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W3761872",
                    "item_ids": ["3019027053", "3709608322"],
                    "new_item_ids": ["7958300294", "7806008610"],
                    "payment_method_id": "credit_card_7287775",
                },
            )
        ],
        outputs=[],
    
        uuid="a78e7220-d0ff-45ab-af8b-80c6f8079995",),
    Task(
        annotator="synthetic",
        user_id="sofia_thomas_1518",
        instruction="Your name is Sofia Thomas and your zip code is 75307. You are curious, shy. For #W2297866, modify Vacuum Cleaner {'type': 'upright', 'bagged/bagless': 'bagless', 'features': 'HEPA filter'} to {'type': 'robotic', 'features': 'pet hair removal'}; via paypal_5334408. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W2297866",
                    "item_ids": ["7407609582"],
                    "new_item_ids": ["4965355367"],
                    "payment_method_id": "paypal_5334408",
                },
            )
        ],
        outputs=[],
    
        uuid="a320bf05-da55-4825-9495-ce17a45c6dd9",),
    Task(
        annotator="synthetic",
        user_id="sophia_martin_8570",
        instruction="Your name is Sophia Martin and your zip code is 77034. You are relaxing, happy, insecure, impatient. For #W1603792, change address to {'order_id': '#W1603792', 'address1': '592 Elm Avenue', 'address2': 'Suite 978', 'city': 'Houston', 'country': 'USA', 'state': 'TX', 'zip': '77242'} (same as #W1092119). For #W1603792, modify Bicycle {'frame size': 'large', 'color': 'red', 'type': 'mountain'} to {'frame size': 'medium', 'color': 'black'}; via credit_card_5694100. ",
        actions=[
            Action(
                name="modify_pending_order_address",
                kwargs={
                    "order_id": "#W1603792",
                    "address1": "592 Elm Avenue",
                    "address2": "Suite 978",
                    "city": "Houston",
                    "country": "USA",
                    "state": "TX",
                    "zip": "77242",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W1603792",
                    "item_ids": ["5606522780"],
                    "new_item_ids": ["2143041831"],
                    "payment_method_id": "credit_card_5694100",
                },
            ),
        ],
        outputs=[],
    
        uuid="8d06532b-866f-48ff-8120-d6ed69d21f7c",),
    Task(
        annotator="synthetic",
        user_id="emma_kovacs_5477",
        instruction="Your name is Emma Kovacs and your zip code is 95111. You are shy, creative. For #W6554908, change address to {'order_id': '#W6554908', 'address1': '111 Sunset Drive', 'address2': 'Suite 183', 'city': 'San Diego', 'country': 'USA', 'state': 'CA', 'zip': '92179'} (same as #W3618959). For #W6554908, modify Skateboard {'deck material': 'maple', 'length': '28 inch', 'design': 'graphic'} to {'deck material': 'plastic', 'design': 'plain'}; Perfume {'scent family': 'fresh', 'size': '30ml', 'gender': 'men'} to {'scent family': 'oriental'}; via gift_card_9246707. For #W7109609, change address to {'order_id': '#W7109609', 'address1': '111 Sunset Drive', 'address2': 'Suite 183', 'city': 'San Diego', 'country': 'USA', 'state': 'CA', 'zip': '92179'} (same as #W3618959). For #W7109609, modify Vacuum Cleaner {'type': 'robotic', 'bagged/bagless': 'bagless', 'features': 'cordless'} to {'features': 'pet hair removal'}; via gift_card_9246707. ",
        actions=[
            Action(
                name="modify_pending_order_address",
                kwargs={
                    "order_id": "#W6554908",
                    "address1": "111 Sunset Drive",
                    "address2": "Suite 183",
                    "city": "San Diego",
                    "country": "USA",
                    "state": "CA",
                    "zip": "92179",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W6554908",
                    "item_ids": ["2819462352", "9447903288"],
                    "new_item_ids": ["4545791457", "1325156478"],
                    "payment_method_id": "gift_card_9246707",
                },
            ),
            Action(
                name="modify_pending_order_address",
                kwargs={
                    "order_id": "#W7109609",
                    "address1": "111 Sunset Drive",
                    "address2": "Suite 183",
                    "city": "San Diego",
                    "country": "USA",
                    "state": "CA",
                    "zip": "92179",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W7109609",
                    "item_ids": ["4806644905"],
                    "new_item_ids": ["4965355367"],
                    "payment_method_id": "gift_card_9246707",
                },
            ),
        ],
        outputs=[],
    
        uuid="3c286ef8-7648-4388-8382-b3b682c09430",),
    Task(
        annotator="synthetic",
        user_id="evelyn_patel_8882",
        instruction="Your name is Evelyn Patel and your email is evelyn.patel2010@example.com. You are independent, cautious, relaxing, happy, messy. For #W6385395, modify T-Shirt {'color': 'purple', 'size': 'XL', 'material': 'cotton', 'style': 'crew neck'} to {'color': 'blue', 'size': 'M'}; Fleece Jacket {'size': 'S', 'color': 'red', 'zipper': 'half'} to {'size': 'L'}; via paypal_3704667. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W6385395",
                    "item_ids": ["8124970213", "5992316252"],
                    "new_item_ids": ["9612497925", "8733974883"],
                    "payment_method_id": "paypal_3704667",
                },
            )
        ],
        outputs=[],
    
        uuid="6a00217b-2482-4c01-aab5-31e2abac6e5f",),
    Task(
        annotator="synthetic",
        user_id="mason_wilson_4597",
        instruction="Your name is Mason Wilson and your zip code is 85028. You are confident, optimistic, polite. For #W4318885, modify Bluetooth Speaker {'color': 'blue', 'battery life': '10 hours', 'water resistance': 'yes'} to {'battery life': '20 hours', 'water resistance': 'no'}; via gift_card_6767859. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W4318885",
                    "item_ids": ["4716977452"],
                    "new_item_ids": ["2635605237"],
                    "payment_method_id": "gift_card_6767859",
                },
            )
        ],
        outputs=[],
    
        uuid="f12362c8-fa1c-4e4f-8a66-91ade1e42ba5",),
    Task(
        annotator="synthetic",
        user_id="omar_santos_4830",
        instruction="Your name is Omar Santos and your email is omar.santos1752@example.com. You are flexible, patient. For #W9121070, change payment to credit_card_8992222. For #W9121070, modify Backpack {'color': 'black', 'size': 'medium', 'material': 'nylon', 'compartment': 'hydration'} to {'color': 'green', 'size': 'small', 'material': 'polyester', 'compartment': 'camera'}; via gift_card_3895897. ",
        actions=[
            Action(
                name="modify_pending_order_payment",
                kwargs={
                    "order_id": "#W9121070",
                    "payment_method_id": "credit_card_8992222",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W9121070",
                    "item_ids": ["8030558068"],
                    "new_item_ids": ["9851293632"],
                    "payment_method_id": "gift_card_3895897",
                },
            ),
        ],
        outputs=[],
    
        uuid="df19a1da-db40-4c65-bd27-c59ea09bdac9",),
    
    
    Task(
        annotator="synthetic",
        user_id="ava_lopez_2676",
        instruction="Your name is Ava Lopez and your email is ava.lopez3569@example.com. You are optimistic, direct. For #W5911003, change payment to gift_card_4855547. For #W5911003, modify Garden Hose {'length': '100ft', 'material': 'rubber', 'color': 'black'} to {'length': '50ft', 'material': 'vinyl'}; Office Chair {'material': 'mesh', 'color': 'red', 'armrest': 'none', 'backrest height': 'standard'} to {'material': 'leather', 'color': 'blue', 'backrest height': 'high-back'}; Hiking Boots {'size': '10', 'material': 'leather', 'waterproof': 'no'} to {'size': '12', 'material': 'synthetic'}; via credit_card_7772870. For #W8327915, change address to {'order_id': '#W8327915', 'address1': '836 Hickory Lane', 'address2': 'Suite 848', 'city': 'San Diego', 'country': 'USA', 'state': 'CA', 'zip': '92168'} (same as #W5911003). For #W8327915, change payment to credit_card_7772870. For #W8327915, modify Sunglasses {'frame color': 'black', 'lens color': 'brown', 'lens type': 'polarized', 'frame material': 'plastic'} to {'frame color': 'brown'}; Skateboard {'deck material': 'bamboo', 'length': '34 inch', 'design': 'custom'} to {'length': '28 inch'}; via gift_card_4855547. ",
        actions=[
            Action(
                name="modify_pending_order_payment",
                kwargs={
                    "order_id": "#W5911003",
                    "payment_method_id": "gift_card_4855547",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W5911003",
                    "item_ids": ["1518544029", "4274709903", "2185126308"],
                    "new_item_ids": ["5206946487", "8069050545", "4582956489"],
                    "payment_method_id": "credit_card_7772870",
                },
            ),
            Action(
                name="modify_pending_order_address",
                kwargs={
                    "order_id": "#W8327915",
                    "address1": "836 Hickory Lane",
                    "address2": "Suite 848",
                    "city": "San Diego",
                    "country": "USA",
                    "state": "CA",
                    "zip": "92168",
                },
            ),
            Action(
                name="modify_pending_order_payment",
                kwargs={
                    "order_id": "#W8327915",
                    "payment_method_id": "credit_card_7772870",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W8327915",
                    "item_ids": ["4358482460", "6956751343"],
                    "new_item_ids": ["9672174103", "6673921677"],
                    "payment_method_id": "gift_card_4855547",
                },
            ),
        ],
        outputs=[],
    
        uuid="0b19fd50-8055-461c-bac8-a0412031c4f7",),
    Task(
        annotator="synthetic",
        user_id="raj_lopez_5873",
        instruction="Your name is Raj Lopez and your zip code is 76195. You are flexible, shy. For #W5107138, change payment to paypal_7007375. For #W5107138, modify Grill {'type': 'electric', 'size': 'medium', 'features': 'side burner'} to {'type': 'charcoal', 'features': 'rotisserie'}; via credit_card_6731308. ",
        actions=[
            Action(
                name="modify_pending_order_payment",
                kwargs={
                    "order_id": "#W5107138",
                    "payment_method_id": "paypal_7007375",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W5107138",
                    "item_ids": ["5666020311"],
                    "new_item_ids": ["7082455361"],
                    "payment_method_id": "credit_card_6731308",
                },
            ),
        ],
        outputs=[],
    
        uuid="59d920a7-cf15-4bd9-946c-6afca1d7fe38",),
    Task(
        annotator="synthetic",
        user_id="harper_kim_2998",
        instruction="Your name is Harper Kim and your zip code is 78222. You are polite, creative, messy, confident. For #W7807988, modify Digital Camera {'resolution': '24MP', 'zoom': '3x', 'storage': 'SD card'} to {'resolution': '30MP'}; via gift_card_5328393. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W7807988",
                    "item_ids": ["5996159312"],
                    "new_item_ids": ["1804581713"],
                    "payment_method_id": "gift_card_5328393",
                },
            )
        ],
        outputs=[],
    
        uuid="893d3404-8f7d-4870-8f0a-3dbefa2affb0",),
    Task(
        annotator="synthetic",
        user_id="ethan_santos_6104",
        instruction="Your name is Ethan Santos and your zip code is 80278. You are insecure, happy. For #W5320242, modify Cycling Helmet {'size': 'L', 'color': 'blue', 'ventilation': 'high'} to {'size': 'S', 'color': 'white', 'ventilation': 'medium'}; Tablet {'screen size': '7-inch', 'storage': '128GB', 'color': 'black'} to {'screen size': '10-inch', 'storage': '64GB', 'color': 'silver'}; via credit_card_9784468. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W5320242",
                    "item_ids": ["2206116040", "4913411651"],
                    "new_item_ids": ["7811981098", "2106335193"],
                    "payment_method_id": "credit_card_9784468",
                },
            )
        ],
        outputs=[],
    
        uuid="74321f09-a18d-49b1-8614-5a78957f11e2",),
    
    Task(
        annotator="synthetic",
        user_id="amelia_ito_8772",
        instruction="Your name is Amelia Ito and your email is amelia.ito8974@example.com. You are polite, logical, sad, impatient, busy. For #W3883329, modify Cycling Helmet {'size': 'S', 'color': 'black', 'ventilation': 'medium'} to {'color': 'red', 'ventilation': 'low'}; Digital Camera {'resolution': '30MP', 'zoom': '3x', 'storage': 'CF card'} to {'resolution': '24MP', 'storage': 'SD card'}; via paypal_2767694. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W3883329",
                    "item_ids": ["5537798301", "7255224608"],
                    "new_item_ids": ["3358616356", "5996159312"],
                    "payment_method_id": "paypal_2767694",
                },
            )
        ],
        outputs=[],
    
        uuid="9da2753b-a2b5-45db-8cd0-778deb0cbab7",),
    Task(
        annotator="synthetic",
        user_id="fatima_brown_2588",
        instruction="Your name is Fatima Brown and your email is fatima.brown8196@example.com. You are flexible, direct, cautious. For #W8008214, modify Espresso Machine {'pressure': '9 bar', 'capacity': '1L', 'type': 'capsule'} to {'pressure': '19 bar', 'capacity': '1.5L', 'type': 'automatic'}; via paypal_8445813. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W8008214",
                    "item_ids": ["7806008610"],
                    "new_item_ids": ["3951031513"],
                    "payment_method_id": "paypal_8445813",
                },
            )
        ],
        outputs=[],
    
        uuid="050bf0f7-b383-4a4b-8167-d6cf03ce651f",),
    Task(
        annotator="synthetic",
        user_id="ethan_santos_6104",
        instruction="Your name is Ethan Santos and your email is ethan.santos9082@example.com. You are outgoing, pessimistic, independent. For #W5320242, change address to {'order_id': '#W5320242', 'address1': '654 Spruce Street', 'address2': 'Suite 503', 'city': 'Denver', 'country': 'USA', 'state': 'CO', 'zip': '80278'} (same as #W4642822). For #W5320242, modify Indoor Security Camera {'resolution': '2K', 'field of view': '160 degrees', 'connectivity': 'Ethernet'} to {'resolution': '4K', 'field of view': '130 degrees'}; Luggage Set {'piece count': '2-piece', 'color': 'red', 'material': 'softshell'} to {'piece count': '4-piece', 'material': 'hardshell'}; via credit_card_9784468. ",
        actions=[
            Action(
                name="modify_pending_order_address",
                kwargs={
                    "order_id": "#W5320242",
                    "address1": "654 Spruce Street",
                    "address2": "Suite 503",
                    "city": "Denver",
                    "country": "USA",
                    "state": "CO",
                    "zip": "80278",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W5320242",
                    "item_ids": ["5966895767", "7160999700"],
                    "new_item_ids": ["6901578702", "9956648681"],
                    "payment_method_id": "credit_card_9784468",
                },
            ),
        ],
        outputs=[],
    
        uuid="dad383a5-84d9-4365-b4ec-1fe118c3e1d2",),
    Task(
        annotator="synthetic",
        user_id="chen_ahmed_3232",
        instruction="Your name is Chen Ahmed and your zip code is 46210. You are independent, flexible, curious, impatient, direct. For #W8268544, modify Cycling Helmet {'size': 'L', 'color': 'red', 'ventilation': 'high'} to {'size': 'S', 'color': 'white', 'ventilation': 'low'}; Smartphone {'color': 'gold', 'storage': '128GB', 'RAM': '6GB', 'screen size': '6.1-inch'} to {'color': 'black', 'RAM': '8GB', 'screen size': '5.8-inch'}; via gift_card_1402922. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W8268544",
                    "item_ids": ["7401244629", "1631373418"],
                    "new_item_ids": ["1596993217", "1507389580"],
                    "payment_method_id": "gift_card_1402922",
                },
            )
        ],
        outputs=[],
    
        uuid="9acdc9f6-9acd-4183-834c-d91f290da855",),
    Task(
        annotator="synthetic",
        user_id="anya_patel_3710",
        instruction="Your name is Anya Patel and your email is anya.patel9309@example.com. You are rigid, organized. For #W4604258, change payment to credit_card_4142574. For #W4604258, modify Bluetooth Speaker {'color': 'black', 'battery life': '10 hours', 'water resistance': 'yes'} to {'color': 'red', 'battery life': '20 hours', 'water resistance': 'no'}; Tea Kettle {'material': 'ceramic', 'capacity': '1.5 liters', 'stovetop compatibility': 'gas'} to {'material': 'glass', 'capacity': '1 liter', 'stovetop compatibility': 'electric'}; Hiking Boots {'size': '8', 'material': 'leather', 'waterproof': 'yes'} to {'size': '11', 'waterproof': 'no'}; via credit_card_4142574. ",
        actions=[
            Action(
                name="modify_pending_order_payment",
                kwargs={
                    "order_id": "#W4604258",
                    "payment_method_id": "credit_card_4142574",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W4604258",
                    "item_ids": ["5855700373", "7497340597", "2648909398"],
                    "new_item_ids": ["1052700637", "9747045638", "5676696062"],
                    "payment_method_id": "credit_card_4142574",
                },
            ),
        ],
        outputs=[],
    
        uuid="a7062bbe-75d7-456e-8f16-c20d0e53be0e",),
    Task(
        annotator="synthetic",
        user_id="evelyn_kovacs_6742",
        instruction="Your name is Evelyn Kovacs and your zip code is 32117. You are optimistic, cautious, dependent, direct. For #W6689278, change address to {'order_id': '#W6689278', 'address1': '505 Cedar Avenue', 'address2': 'Suite 539', 'city': 'Jacksonville', 'country': 'USA', 'state': 'FL', 'zip': '32117'} (same as #W5694685). For #W6689278, modify Electric Kettle {'capacity': '1L', 'material': 'plastic', 'color': 'white'} to {'capacity': '1.5L', 'color': 'silver'}; via paypal_7732922. For #W7398274, change address to {'order_id': '#W7398274', 'address1': '295 Elm Avenue', 'address2': 'Suite 793', 'city': 'Los Angeles', 'country': 'USA', 'state': 'CA', 'zip': '90320'} (same as #W2768683). For #W7398274, modify Notebook {'size': 'A4', 'cover type': 'hard cover'} to {}; Wristwatch {'strap material': 'leather', 'dial color': 'white'} to {'strap material': 'metal', 'dial color': 'black'}; via paypal_7732922. ",
        actions=[
            Action(
                name="modify_pending_order_address",
                kwargs={
                    "order_id": "#W6689278",
                    "address1": "505 Cedar Avenue",
                    "address2": "Suite 539",
                    "city": "Jacksonville",
                    "country": "USA",
                    "state": "FL",
                    "zip": "32117",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W6689278",
                    "item_ids": ["2243454707"],
                    "new_item_ids": ["9624127908"],
                    "payment_method_id": "paypal_7732922",
                },
            ),
            Action(
                name="modify_pending_order_address",
                kwargs={
                    "order_id": "#W7398274",
                    "address1": "295 Elm Avenue",
                    "address2": "Suite 793",
                    "city": "Los Angeles",
                    "country": "USA",
                    "state": "CA",
                    "zip": "90320",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W7398274",
                    "item_ids": ["1199058591", "1355937109"],
                    "new_item_ids": ["1199058591", "4510078629"],
                    "payment_method_id": "paypal_7732922",
                },
            ),
        ],
        outputs=[],
    
        uuid="e2761e49-1fee-47d1-811f-cb47437a15ed",),
    
    Task(
        annotator="synthetic",
        user_id="juan_lopez_5820",
        instruction="Your name is Juan Lopez and your zip code is 85060. You are patient, dependent, shy, rigid, busy. For #W3386832, modify Espresso Machine {'pressure': '9 bar', 'capacity': '2L', 'type': 'automatic'} to {'type': 'manual'}; Cycling Helmet {'size': 'M', 'color': 'blue', 'ventilation': 'low'} to {'color': 'red', 'ventilation': 'medium'}; via paypal_6729210. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W3386832",
                    "item_ids": ["3709608322", "3339188619"],
                    "new_item_ids": ["7774234341", "1719127154"],
                    "payment_method_id": "paypal_6729210",
                },
            )
        ],
        outputs=[],
    
        uuid="bf142add-a412-4475-bc2a-bfbadaa0e4c7",),
    Task(
        annotator="synthetic",
        user_id="evelyn_ahmed_3960",
        instruction="Your name is Evelyn Ahmed and your zip code is 80256. You are messy, creative, direct, outgoing, sad. For #W3746173, change payment to credit_card_7898168. For #W3746173, modify Makeup Kit {'skin tone': 'medium', 'kit size': 'professional', 'brand': 'Brand A'} to {'skin tone': 'dark', 'brand': 'Brand C'}; via gift_card_5683713. ",
        actions=[
            Action(
                name="modify_pending_order_payment",
                kwargs={
                    "order_id": "#W3746173",
                    "payment_method_id": "credit_card_7898168",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W3746173",
                    "item_ids": ["2882812427"],
                    "new_item_ids": ["1763705424"],
                    "payment_method_id": "gift_card_5683713",
                },
            ),
        ],
        outputs=[],
    
        uuid="5012eaa6-118d-4ca3-8d2a-587fe2b7a889",),
    
    Task(
        annotator="synthetic",
        user_id="daiki_li_8218",
        instruction="Your name is Daiki Li and your zip code is 75201. You are creative, impatient, curious. For #W6958840, change payment to gift_card_5730441. For #W6958840, modify Cycling Helmet {'size': 'L', 'color': 'black', 'ventilation': 'low'} to {'size': 'S', 'ventilation': 'medium'}; via credit_card_1687024. ",
        actions=[
            Action(
                name="modify_pending_order_payment",
                kwargs={
                    "order_id": "#W6958840",
                    "payment_method_id": "gift_card_5730441",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W6958840",
                    "item_ids": ["6048672633"],
                    "new_item_ids": ["5537798301"],
                    "payment_method_id": "credit_card_1687024",
                },
            ),
        ],
        outputs=[],
    
        uuid="32e55e9e-05b3-49ce-b708-593797f18a9a",),
    Task(
        annotator="synthetic",
        user_id="james_lee_5010",
        instruction="Your name is James Lee and your zip code is 95161. You are messy, confident, direct, shy, busy. For #W5356919, change address to {'order_id': '#W5356919', 'address1': '870 Oak Street', 'address2': 'Suite 766', 'city': 'San Jose', 'country': 'USA', 'state': 'CA', 'zip': '95161'} (same as #W8520591). For #W5356919, modify Jigsaw Puzzle {'pieces': '1000', 'theme': 'art', 'difficulty level': 'expert'} to {'pieces': '500', 'difficulty level': 'intermediate'}; via paypal_2684483. ",
        actions=[
            Action(
                name="modify_pending_order_address",
                kwargs={
                    "order_id": "#W5356919",
                    "address1": "870 Oak Street",
                    "address2": "Suite 766",
                    "city": "San Jose",
                    "country": "USA",
                    "state": "CA",
                    "zip": "95161",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W5356919",
                    "item_ids": ["9370300555"],
                    "new_item_ids": ["4068787148"],
                    "payment_method_id": "paypal_2684483",
                },
            ),
        ],
        outputs=[],
    
        uuid="ee4603c4-a671-4000-8169-24d05d120f82",),
    Task(
        annotator="synthetic",
        user_id="noah_patel_6952",
        instruction="Your name is Noah Patel and your zip code is 10108. You are polite, messy. For #W1845024, modify Office Chair {'material': 'fabric', 'color': 'blue', 'armrest': 'adjustable', 'backrest height': 'standard'} to {}; via paypal_3169710. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W1845024",
                    "item_ids": ["8323284863"],
                    "new_item_ids": ["8323284863"],
                    "payment_method_id": "paypal_3169710",
                },
            )
        ],
        outputs=[],
    
        uuid="0fba2ae9-9b91-4c9a-99b2-f009e26ac6d4",),
    Task(
        annotator="synthetic",
        user_id="olivia_sanchez_2914",
        instruction="Your name is Olivia Sanchez and your email is olivia.sanchez1894@example.com. You are flexible, logical, sad. For #W5101035, modify Electric Toothbrush {'color': 'black', 'speed settings': 'high', 'battery type': 'AA batteries'} to {'battery type': 'rechargeable'}; via paypal_3388537. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W5101035",
                    "item_ids": ["8798690242"],
                    "new_item_ids": ["8098621301"],
                    "payment_method_id": "paypal_3388537",
                },
            )
        ],
        outputs=[],
    
        uuid="d9afc8b0-958f-4633-82a2-fb7dcf8f4fb7",),
    Task(
        annotator="synthetic",
        user_id="lucas_muller_4380",
        instruction="Your name is Lucas Muller and your zip code is 78763. You are patient, direct, relaxing, flexible, pessimistic. For #W3206099, modify Dumbbell Set {'weight range': '30-50 lbs', 'material': 'iron', 'set type': 'fixed'} to {'weight range': '5-25 lbs', 'material': 'urethane'}; via gift_card_2748512. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W3206099",
                    "item_ids": ["3333391894"],
                    "new_item_ids": ["6585768447"],
                    "payment_method_id": "gift_card_2748512",
                },
            )
        ],
        outputs=[],
    
        uuid="58aed92b-3870-49aa-bc67-2523f2c27dc9",),
    Task(
        annotator="synthetic",
        user_id="noah_hernandez_4232",
        instruction="Your name is Noah Hernandez and your email is noah.hernandez4161@example.com. You are insecure, pessimistic, relaxing, patient. For #W3897284, modify E-Reader {'screen size': '7-inch', 'connectivity': 'Wi-Fi + Cellular', 'storage': '8GB'} to {'storage': '32GB'}; via gift_card_3410768. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W3897284",
                    "item_ids": ["5418781403"],
                    "new_item_ids": ["4273929280"],
                    "payment_method_id": "gift_card_3410768",
                },
            )
        ],
        outputs=[],
    
        uuid="b3ac958a-27b4-4f9b-b05b-cc6b903f663d",),
    
    Task(
        annotator="synthetic",
        user_id="liam_li_8526",
        instruction="Your name is Liam Li and your zip code is 28226. You are insecure, logical, cautious, independent, shy. For #W1130240, change address to {'order_id': '#W1130240', 'address1': '707 Maple Drive', 'address2': 'Suite 817', 'city': 'San Antonio', 'country': 'USA', 'state': 'TX', 'zip': '78202'} (same as #W8838515). For #W1130240, modify Dumbbell Set {'weight range': '30-50 lbs', 'material': 'urethane', 'set type': 'fixed'} to {'weight range': '5-25 lbs', 'material': 'rubber'}; via gift_card_5427896. ",
        actions=[
            Action(
                name="modify_pending_order_address",
                kwargs={
                    "order_id": "#W1130240",
                    "address1": "707 Maple Drive",
                    "address2": "Suite 817",
                    "city": "San Antonio",
                    "country": "USA",
                    "state": "TX",
                    "zip": "78202",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W1130240",
                    "item_ids": ["7159180318"],
                    "new_item_ids": ["8068777068"],
                    "payment_method_id": "gift_card_5427896",
                },
            ),
        ],
        outputs=[],
    
        uuid="e76d6143-743d-4d2e-a5e9-8c32b16994fe",),
    Task(
        annotator="synthetic",
        user_id="sofia_kovacs_7075",
        instruction="Your name is Sofia Kovacs and your zip code is 19049. You are organized, patient, independent, outgoing, pessimistic. For #W5765741, modify Portable Charger {'capacity': '5000mAh', 'output': 'USB-A', 'color': 'white'} to {'capacity': '20000mAh', 'output': 'Wireless', 'color': 'black'}; via paypal_6840891. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W5765741",
                    "item_ids": ["7903094618"],
                    "new_item_ids": ["8349903180"],
                    "payment_method_id": "paypal_6840891",
                },
            )
        ],
        outputs=[],
    
        uuid="9269a099-3b40-4f69-83fa-b89fdec4d2ef",),
    Task(
        annotator="synthetic",
        user_id="ethan_moore_3587",
        instruction="Your name is Ethan Moore and your zip code is 90651. You are optimistic, shy. For #W7584328, modify Backpack {'color': 'navy', 'size': 'small', 'material': 'nylon', 'compartment': 'laptop'} to {'color': 'green', 'material': 'leather', 'compartment': 'camera'}; via credit_card_6173085. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W7584328",
                    "item_ids": ["2492465580"],
                    "new_item_ids": ["7251508981"],
                    "payment_method_id": "credit_card_6173085",
                },
            )
        ],
        outputs=[],
    
        uuid="7bfb2298-71f0-40a6-b687-162696f688ac",),
    Task(
        annotator="synthetic",
        user_id="liam_li_5260",
        instruction="Your name is Liam Li and your email is liam.li2557@example.com. You are curious, relaxing, insecure, creative, outgoing. For #W9653558, modify Coffee Maker {'color': 'stainless steel', 'capacity': '4 cups', 'type': 'drip', 'features': 'built-in grinder'} to {'color': 'black', 'capacity': '2 cups', 'type': 'espresso', 'features': 'timer'}; via credit_card_7933535. ",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W9653558",
                    "item_ids": ["1323134954"],
                    "new_item_ids": ["9862136885"],
                    "payment_method_id": "credit_card_7933535",
                },
            )
        ],
        outputs=[],
    
        uuid="21c05d13-b5ba-4980-b930-ca070204ff0c",),
    Task(
        annotator="",
        user_id="james_kim_7213",
        instruction="Your name is James Kim and your zip code is 92199. You are relaxing, polite, independent, pessimistic, confident. For #W3289292, change address to {'order_id': '#W3289292', 'address1': '320 Cedar Avenue', 'address2': 'Suite 116', 'city': 'San Antonio', 'country': 'USA', 'state': 'TX', 'zip': '78219'} (same as #W9154975). For #W3289292, exchange Mechanical Keyboard {'switch type': 'clicky', 'backlight': 'RGB', 'size': 'full size'} to {'switch type': 'linear'}; ",
        actions=[
            Action(
                name="modify_pending_order_address",
                kwargs={
                    "order_id": "#W3289292",
                    "address1": "320 Cedar Avenue",
                    "address2": "Suite 116",
                    "city": "San Antonio",
                    "country": "USA",
                    "state": "TX",
                    "zip": "78219",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W3289292",
                    "item_ids": ["9025753381"],
                    "new_item_ids": ["1151293680"],
                    "payment_method_id": "paypal_8963303",
                },
            ),
        ],
        outputs=[],
    
        uuid="a47826f1-1a98-4f5a-9819-8a7d57c1444c",),
    Task(
        annotator="",
        user_id="fatima_anderson_2157",
        instruction="Your name is Fatima Anderson and your zip code is 32100. You are relaxing, logical, shy, polite. For the #W2974929 that you've just placed, you realize that you've picked the wrong deck material, change it to 'bamboo' deck material.",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W2974929",
                    "item_ids": ["3877188862"],
                    "new_item_ids": ["4293355847"],
                    "payment_method_id": "paypal_7916550",
                },
            )
        ],
        outputs=[],
    
        uuid="d2673027-b163-4ae8-a39d-9606d46d717f",),
    Task(
        annotator="",
        user_id="omar_silva_7446",
        instruction="Your name is Omar Silva and your zip code is 92107. You are messy, curious, busy. For #W9673784 order that you've placed you'd like to exchange 19 bar Espresso Machine that you've placed to a 9 bar capsule espresso machine. If the agent asks for payment or refund method, you prefer paypal than GC.",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W9673784",
                    "item_ids": ["9884666842"],
                    "new_item_ids": ["7806008610"],
                    "payment_method_id": "paypal_2192303",
                },
            )
        ],
        outputs=[],
    
        uuid="0c61db90-86bd-4ed3-8b67-a3c12db38aaf",),
    Task(
        annotator="4",
        user_id="yara_silva_7567",
        instruction="You name is Yara Silva and your zip code is 77159. You are sad and cautious. You want to modify the laptop order to your NYC address (you don't want to reveal it but should be in your orders profile). You also like to modify the laptop to be {'processor': 'i5', 'storage': '256GB SSD', 'color': 'space grey'};  You also want to exchange your watch to be black dial color but keep the leather strap. You like to say things together.",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W9810810",
                    "item_ids": ["1355937109"],
                    "new_item_ids": ["9949163720"],
                    "payment_method_id": "gift_card_7252880",
                },
            ),
            Action(
                name="modify_pending_order_address",
                kwargs={
                    "order_id": "#W3730488",
                    "address1": "555 Highland Drive",
                    "address2": "Suite 872",
                    "city": "New York",
                    "country": "USA",
                    "state": "NY",
                    "zip": "10116",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W3730488",
                    "item_ids": ["2913673670"],
                    "new_item_ids": ["2216662955"],
                    "payment_method_id": "gift_card_7252880",
                },
            ),
        ],
        outputs=[],
    
        uuid="07542e01-278d-4b1e-8a42-bd42e47fd270",),
    Task(
        annotator="4",
        user_id="yara_silva_7567",
        instruction="You name is Yara Silva and your zip code is 77159. You are sad and cautious. You want to modify the laptop order to your NYC address (you don't want to reveal it but should be in your orders profile). You also like to modify the laptop to be 9844888101. You also want to exchange your watch to be black dial color but keep the leather strap. You like to say things piecewise.",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W9810810",
                    "item_ids": ["1355937109"],
                    "new_item_ids": ["9949163720"],
                    "payment_method_id": "gift_card_7252880",
                },
            ),
            Action(
                name="modify_pending_order_address",
                kwargs={
                    "order_id": "#W3730488",
                    "address1": "555 Highland Drive",
                    "address2": "Suite 872",
                    "city": "New York",
                    "country": "USA",
                    "state": "NY",
                    "zip": "10116",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W3730488",
                    "item_ids": ["2913673670"],
                    "new_item_ids": ["9844888101"],
                    "payment_method_id": "gift_card_7252880",
                },
            ),
        ],
        outputs=[],
    
        uuid="5dabdbc0-d9ce-40a0-a87c-35e4f1db58cd",),
    Task(
        annotator="4",
        user_id="ivan_khan_7475",
        instruction="You name is Ivan Khan and your zip code is 28243. You are polite, optimistic, organized. You made some mistake and ordered an order sent to your son's address in Washington DC, and you want to modify it to your default address in Charlotte (you do not want to mention it, but it is in your user profile the agent can look up) because he is coming back home. You also want to adjust the desk lamp to be black color, and the backpack to be medium size and polyester material instead. If multiple colors are available for the backpack, you prefer grey. If the agent asks for payment method, you say GC initially, but if the agent does not allow it or asks you to confirm it, you change your mind to PayPal, and decide to only modify the backpack.",
        actions=[
            Action(
                name="modify_pending_order_address",
                kwargs={
                    "order_id": "#W5270061",
                    "address1": "159 Hickory Lane",
                    "address2": "Suite 995",
                    "city": "Charlotte",
                    "country": "USA",
                    "state": "NC",
                    "zip": "28243",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W5270061",
                    "item_ids": ["2492465580"],
                    "new_item_ids": ["5917587651"],
                    "payment_method_id": "paypal_7729105",
                },
            ),
        ],
        outputs=[],
    
        uuid="46bcd7ae-9094-48e8-93b5-c6674a1033c7",),
    Task(
        annotator="4",
        user_id="ivan_khan_7475",
        instruction="You name is Ivan Khan and your zip code is 28243. You are polite, optimistic, organized. You made some mistake and ordered an order sent to your son's address in Washington DC, and you want to modify it to your default address in Charlotte (you do not want to mention it, but it is in your user profile the agent can look up) because he is coming back home. You also want to adjust the desk lamp to be black color, and the backpack to be medium size and polyester material instead. If multiple colors are available for the backpack, you prefer grey. If the agent asks for payment method, you say GC initially, but if the agent does not allow it or asks you to confirm it, you change your mind to PayPal, and decide to only modify the backpack. Make sure you briefly mention the two things at the same time at the beginning, but first mention the modification then the address.",
        actions=[
            Action(
                name="modify_pending_order_address",
                kwargs={
                    "order_id": "#W5270061",
                    "address1": "159 Hickory Lane",
                    "address2": "Suite 995",
                    "city": "Charlotte",
                    "country": "USA",
                    "state": "NC",
                    "zip": "28243",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W5270061",
                    "item_ids": ["2492465580"],
                    "new_item_ids": ["5917587651"],
                    "payment_method_id": "paypal_7729105",
                },
            ),
        ],
        outputs=[],
    
        uuid="4f163a6e-7e14-4b82-9df6-8cc84bf5e492",),
    Task(
        annotator="4",
        user_id="emma_kovacs_9839",
        instruction="You name is Emma Kovacs and your zip code is 32190. You are insecure, rigid, sad, logical. You just bought a water bottle with 500ml but you regret it, and you want to change it to the other bottle you just placed with 1000ml capacity. If the exact item is not available any more, you can allow the material to be different.",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W8661412",
                    "item_ids": ["3453331371"],
                    "new_item_ids": ["2439754078"],
                    "payment_method_id": "credit_card_7239357",
                },
            )
        ],
        outputs=[],
    
        uuid="a4a783e4-9708-4035-8ef8-eaf48f2c3bb2",),
    
    
    Task(
        annotator="4",
        user_id="yusuf_hernandez_6785",
        instruction="You name is Yusuf Hernandez and your email is yusuf.hernandez8836@example.com. You are shy, rigid. You want to exchange your Fleece Jacket for a large red Fleece Jacket with a half zipper",
        actions=[
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W2466703",
                    "item_ids": ["9385662952"],
                    "new_item_ids": ["8733974883"],
                    "payment_method_id": "paypal_7529813",
                },
            )
        ],
        outputs=[],
    
        uuid="4bce3704-a38f-452d-afee-477b0aaad852",),
    Task(
        annotator="4",
        user_id="yusuf_li_7255",
        instruction="You name is Yusuf Li and your zip code is 91148. You are cautious, insecure, organized. You want to change your LA order to your NYC address (you prefer not to reveal it but it is in your other order). You also want to exchange Bluetooth Speaker to be the cheapest green type.",
        actions=[
            Action(
                name="modify_pending_order_address",
                kwargs={
                    "order_id": "#W6750959",
                    "address1": "476 Maple Drive",
                    "address2": "Suite 432",
                    "city": "New York",
                    "country": "USA",
                    "state": "NY",
                    "zip": "10093",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W6750959",
                    "item_ids": ["3254583681"],
                    "new_item_ids": ["9440686670"],
                    "payment_method_id": "paypal_8080730",
                },
            ),
        ],
        outputs=[],
    
        uuid="4302c6d2-96a6-4c7f-b24d-31ae9220984b",),
    Task(
        annotator="4",
        user_id="yusuf_li_7255",
        instruction="You name is Yusuf Li and your zip code is 91148. You are cautious, insecure, organized. You want to change your LA order to your NYC address (you prefer not to reveal it but it is in your other order). You also want to exchange Bluetooth Speaker to be the cheapest green type. Make sure you mention the two requests at the same time to the agent, but mention the exchange first.",
        actions=[
            Action(
                name="modify_pending_order_address",
                kwargs={
                    "order_id": "#W6750959",
                    "address1": "476 Maple Drive",
                    "address2": "Suite 432",
                    "city": "New York",
                    "country": "USA",
                    "state": "NY",
                    "zip": "10093",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W6750959",
                    "item_ids": ["3254583681"],
                    "new_item_ids": ["9440686670"],
                    "payment_method_id": "paypal_8080730",
                },
            ),
        ],
        outputs=[],
    
        uuid="8efa6083-d563-4f40-8aed-5009b264cf18",),
    Task(
        annotator="4",
        user_id="noah_ito_3850",
        instruction="You name is Noah Ito and your zip code is 98187. You are logical, impatient. You just placed an order with two watches, you wan to change its address to your New York address (you don't want to reveal it but it's in your other order). You also want to modify the silicone watch to a metal one. If multiple colors available, you prefer white. For the air purifier you received along with a speaker, you want to exchange the purifier to large size and night mode, but still with HEPA filter. You like to say things in pieces.",
        actions=[
            Action(
                name="modify_pending_order_address",
                kwargs={
                    "order_id": "#W4219264",
                    "address1": "144 Lakeview Drive",
                    "address2": "Suite 925",
                    "city": "New York",
                    "country": "USA",
                    "state": "NY",
                    "zip": "10228",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W4219264",
                    "item_ids": ["8886009523"],
                    "new_item_ids": ["2407258246"],
                    "payment_method_id": "credit_card_1620755",
                },
            ),
            Action(
                name="modify_pending_order_items",
                kwargs={
                    "order_id": "#W6729841",
                    "item_ids": ["3076708684"],
                    "new_item_ids": ["8302289002"],
                    "payment_method_id": "credit_card_1620755",
                },
            ),
        ],
        outputs=[],
    
        uuid="9d0af8d1-a249-4ca1-a57a-5c760e1c7f4e",),
    
]