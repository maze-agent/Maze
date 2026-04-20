from tau_bench.types import Task, Action

TASKS_TRAIN = [
    Task(
        annotator="synthetic",
        user_id="omar_anderson_3203",
        instruction="Your name is Omar Anderson and your zip code is 19031. You are logical, independent, relaxing, polite. Return #W6067464 via credit_card_4190576: Electric Kettle; Wall Clock; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W6067464",
                    "item_ids": ["9624127908", "8917609800"],
                    "payment_method_id": "credit_card_4190576",
                },
            )
        ],
        outputs=[],
    
        uuid="db912f98-84ad-4591-90d3-f3087eaea832",),
    Task(
        annotator="synthetic",
        user_id="sophia_nguyen_2370",
        instruction="Your name is Sophia Nguyen and your zip code is 20171. You are confident, organized. Return #W6619432 via paypal_3738584: Dumbbell Set; Yoga Mat; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W6619432",
                    "item_ids": ["3735133539", "6195938807"],
                    "payment_method_id": "paypal_3738584",
                },
            )
        ],
        outputs=[],
    
        uuid="76e903ff-83df-4bad-b073-ca67a4060330",),
    Task(
        annotator="synthetic",
        user_id="james_li_5688",
        instruction="Your name is James Li and your email is james.li4495@example.com. You are rigid, confident, happy, curious, pessimistic. Return #W4435622 via gift_card_1725971: Water Bottle; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W4435622",
                    "item_ids": ["6777246137"],
                    "payment_method_id": "gift_card_1725971",
                },
            )
        ],
        outputs=[],
    
        uuid="dc15e541-6e65-421e-a1f7-9bdf0bccd0dc",),
    Task(
        annotator="synthetic",
        user_id="fatima_taylor_3452",
        instruction="Your name is Fatima Taylor and your zip code is 32169. You are rigid, curious, sad. Return #W5285031 via credit_card_7952624: Tablet; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W5285031",
                    "item_ids": ["2235648106"],
                    "payment_method_id": "credit_card_7952624",
                },
            )
        ],
        outputs=[],
    
        uuid="32356870-8f36-4f90-bdd7-86cf0a2364fc",),
    Task(
        annotator="synthetic",
        user_id="olivia_smith_8953",
        instruction="Your name is Olivia Smith and your email is olivia.smith9157@example.com. You are organized, happy. Return #W3794101 via paypal_2076152: Cycling Helmet; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W3794101",
                    "item_ids": ["3339188619"],
                    "payment_method_id": "paypal_2076152",
                },
            )
        ],
        outputs=[],
    
        uuid="146ff0ca-251c-4a86-b11a-ebec803d008d",),
    Task(
        annotator="synthetic",
        user_id="raj_moore_7909",
        instruction="Your name is Raj Moore and your zip code is 20566. You are happy, outgoing, rigid, optimistic. Return #W3467101 via gift_card_6009199: LED Light Bulb; Headphones; Smart Watch; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W3467101",
                    "item_ids": ["5111440845", "9805150490", "2860956907"],
                    "payment_method_id": "gift_card_6009199",
                },
            )
        ],
        outputs=[],
    
        uuid="662b5be2-ec56-4985-9368-3ef654896ac9",),
    Task(
        annotator="synthetic",
        user_id="ava_nguyen_6971",
        instruction="Your name is Ava Nguyen and your email is ava.nguyen1860@example.com. You are confident, cautious, direct, messy. Return #W7597893 via gift_card_8640626: Smart Thermostat; Mechanical Keyboard; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W7597893",
                    "item_ids": ["9480266227", "9991484137"],
                    "payment_method_id": "gift_card_8640626",
                },
            )
        ],
        outputs=[],
    
        uuid="ed5fa78f-0cf8-4ac0-b0d6-5e83d4128163",),
    Task(
        annotator="synthetic",
        user_id="sophia_garcia_1101",
        instruction="Your name is Sophia Garcia and your email is sophia.garcia9791@example.com. You are patient, messy. Return #W8727985 via gift_card_9450778: Jigsaw Puzzle; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W8727985",
                    "item_ids": ["9030221155"],
                    "payment_method_id": "gift_card_9450778",
                },
            )
        ],
        outputs=[],
    
        uuid="f73cc868-5c54-40fd-9011-9c7003a73b15",),
    Task(
        annotator="synthetic",
        user_id="sofia_ito_5484",
        instruction="Your name is Sofia Ito and your zip code is 19169. You are relaxing, confident, rigid. Return #W5257743 via paypal_6882355: T-Shirt; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W5257743",
                    "item_ids": ["9647292434"],
                    "payment_method_id": "paypal_6882355",
                },
            )
        ],
        outputs=[],
    
        uuid="81cc6036-aa54-4914-ac1f-b9c2226d291d",),
    Task(
        annotator="synthetic",
        user_id="mei_martin_4260",
        instruction="Your name is Mei Martin and your zip code is 32124. You are curious, creative, patient, relaxing, polite. Return #W5564375 via paypal_2299608: Digital Camera; Running Shoes; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W5564375",
                    "item_ids": ["7583936705", "1775591963"],
                    "payment_method_id": "paypal_2299608",
                },
            )
        ],
        outputs=[],
    
        uuid="86ba1a33-d8ce-4700-8ddd-f2931b972d09",),
    Task(
        annotator="synthetic",
        user_id="evelyn_davis_7541",
        instruction="Your name is Evelyn Davis and your zip code is 32136. You are confident, sad. Return #W6798117 via paypal_9734841: Wall Clock; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W6798117",
                    "item_ids": ["6508153405"],
                    "payment_method_id": "paypal_9734841",
                },
            )
        ],
        outputs=[],
    
        uuid="1415db06-3f10-47e4-99c6-7f9952bdf44a",),
    Task(
        annotator="synthetic",
        user_id="omar_muller_7891",
        instruction="Your name is Omar Muller and your email is omar.muller4197@example.com. You are impatient, dependent, logical. Return #W6573840 via gift_card_3689412: Electric Kettle; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W6573840",
                    "item_ids": ["4458619711"],
                    "payment_method_id": "gift_card_3689412",
                },
            )
        ],
        outputs=[],
    
        uuid="b9d21815-45bc-49be-9895-a725ed639a01",),
    Task(
        annotator="synthetic",
        user_id="evelyn_patel_8882",
        instruction="Your name is Evelyn Patel and your email is evelyn.patel2010@example.com. You are direct, insecure, logical, dependent. Return #W9158156 via paypal_3704667: Bluetooth Speaker; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W9158156",
                    "item_ids": ["7751905257"],
                    "payment_method_id": "paypal_3704667",
                },
            )
        ],
        outputs=[],
    
        uuid="ff35dc3a-b449-4525-a3fe-9166c95259bb",),
    Task(
        annotator="synthetic",
        user_id="ethan_johnson_7053",
        instruction="Your name is Ethan Johnson and your email is ethan.johnson2557@example.com. You are shy, rigid, dependent. Return #W5321777 via gift_card_6892585: Espresso Machine; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W5321777",
                    "item_ids": ["7441167885"],
                    "payment_method_id": "gift_card_6892585",
                },
            )
        ],
        outputs=[],
    
        uuid="99456bc1-5bfb-4005-8b6e-4be0550e8833",),
    Task(
        annotator="synthetic",
        user_id="anya_sanchez_9707",
        instruction="Your name is Anya Sanchez and your zip code is 43171. You are messy, busy, outgoing. Return #W4442043 via paypal_1191071: Cycling Helmet; Bicycle; Smartphone; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W4442043",
                    "item_ids": ["6697922351", "7758198585", "3187628796"],
                    "payment_method_id": "paypal_1191071",
                },
            )
        ],
        outputs=[],
    
        uuid="ebc6a858-0665-4da3-9cf3-3af203e39448",),
    Task(
        annotator="synthetic",
        user_id="fatima_anderson_7445",
        instruction="Your name is Fatima Anderson and your email is fatima.anderson1082@example.com. You are impatient, sad, rigid, pessimistic. Return #W1842597 via gift_card_8070316: Running Shoes; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W1842597",
                    "item_ids": ["9791469541"],
                    "payment_method_id": "gift_card_8070316",
                },
            )
        ],
        outputs=[],
    
        uuid="65c1a11d-a8f6-4d5d-8f94-28d505dc17d2",),
    Task(
        annotator="synthetic",
        user_id="amelia_gonzalez_4098",
        instruction="Your name is Amelia Gonzalez and your email is amelia.gonzalez4271@example.com. You are rigid, busy, patient, pessimistic. Return #W7209932 via gift_card_2611937: Backpack; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W7209932",
                    "item_ids": ["5917587651"],
                    "payment_method_id": "gift_card_2611937",
                },
            )
        ],
        outputs=[],
    
        uuid="bb49bf3d-563e-4c29-a154-cd1671469476",),
    Task(
        annotator="synthetic",
        user_id="ethan_thomas_1791",
        instruction="Your name is Ethan Thomas and your zip code is 43188. You are direct, insecure. Return #W7764382 via paypal_6982172: Laptop; Pet Bed; Mechanical Keyboard; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W7764382",
                    "item_ids": ["3334537816", "5067898160", "9665000388"],
                    "payment_method_id": "paypal_6982172",
                },
            )
        ],
        outputs=[],
    
        uuid="73dc86bd-8f07-4418-9491-9a9ab267bdae",),
    Task(
        annotator="synthetic",
        user_id="lei_anderson_8271",
        instruction="Your name is Lei Anderson and your zip code is 76192. You are direct, rigid, optimistic, insecure. Return #W4072946 via paypal_1808675: Hiking Boots; Action Camera; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W4072946",
                    "item_ids": ["8106223139", "5436236388"],
                    "payment_method_id": "paypal_1808675",
                },
            )
        ],
        outputs=[],
    
        uuid="414724d1-7be0-4beb-b1af-0c642a115661",),
    Task(
        annotator="synthetic",
        user_id="aarav_davis_4756",
        instruction="Your name is Aarav Davis and your zip code is 76150. You are insecure, flexible, sad, organized. Return #W3223435 via gift_card_9708163: Electric Kettle; T-Shirt; Garden Hose; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W3223435",
                    "item_ids": ["3015420423", "3799046073", "3230708338"],
                    "payment_method_id": "gift_card_9708163",
                },
            )
        ],
        outputs=[],
    
        uuid="b307d275-0d7d-43ad-bbbf-63d817c2fe73",),
    Task(
        annotator="synthetic",
        user_id="sofia_lee_8857",
        instruction="Your name is Sofia Lee and your email is sofia.lee5283@example.com. You are organized, happy, curious, polite, insecure. Return #W4143549 via paypal_3572679: Indoor Security Camera; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W4143549",
                    "item_ids": ["6867855179"],
                    "payment_method_id": "paypal_3572679",
                },
            )
        ],
        outputs=[],
    
        uuid="e99aa5fb-d2e8-4b58-bba9-207b7a77f5de",),
    Task(
        annotator="synthetic",
        user_id="sofia_li_3261",
        instruction="Your name is Sofia Li and your zip code is 10199. You are optimistic, outgoing, logical, messy, direct. Return #W6874763 via credit_card_4046723: E-Reader; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W6874763",
                    "item_ids": ["9494281769"],
                    "payment_method_id": "credit_card_4046723",
                },
            )
        ],
        outputs=[],
    
        uuid="6a9b4d9a-6310-43e7-849c-e9b3abea69f0",),
    Task(
        annotator="synthetic",
        user_id="sophia_patel_6833",
        instruction="Your name is Sophia Patel and your email is sophia.patel9841@example.com. You are organized, optimistic, confident. Return #W2923184 via credit_card_6419343: Wireless Earbuds; Laptop; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W2923184",
                    "item_ids": ["2757705742", "1684786391"],
                    "payment_method_id": "credit_card_6419343",
                },
            )
        ],
        outputs=[],
    
        uuid="e94896f9-bcb7-41b7-89e9-e20dc4e802b7",),
    Task(
        annotator="synthetic",
        user_id="ava_moore_2033",
        instruction="Your name is Ava Moore and your zip code is 78234. You are busy, creative, messy, sad. Return #W8951014 via gift_card_8168843: Backpack {'color': 'black', 'size': 'small', 'material': 'nylon', 'compartment': 'laptop'}; Bookshelf; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W8951014",
                    "item_ids": ["7824298782", "2244749153"],
                    "payment_method_id": "gift_card_8168843",
                },
            )
        ],
        outputs=[],
    
        uuid="b007204d-fc76-404b-beb3-c7ade868b305",),
    Task(
        annotator="synthetic",
        user_id="raj_santos_9079",
        instruction="Your name is Raj Santos and your zip code is 98157. You are organized, optimistic, dependent. Return #W1630030 via paypal_2417743: Electric Kettle; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W1630030",
                    "item_ids": ["4458619711"],
                    "payment_method_id": "paypal_2417743",
                },
            )
        ],
        outputs=[],
    
        uuid="d72059ef-206e-4ad4-a879-995568d49f5e",),
    Task(
        annotator="synthetic",
        user_id="mason_johansson_8128",
        instruction="Your name is Mason Johansson and your email is mason.johansson9549@example.com. You are shy, dependent. Return #W4352605 via gift_card_1401311: Laptop; Gaming Mouse; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W4352605",
                    "item_ids": ["2216662955", "8214883393"],
                    "payment_method_id": "gift_card_1401311",
                },
            )
        ],
        outputs=[],
    
        uuid="03e37234-9386-44e5-8668-844b690864df",),
    Task(
        annotator="synthetic",
        user_id="lei_anderson_8271",
        instruction="Your name is Lei Anderson and your zip code is 76192. You are busy, impatient, pessimistic, rigid, cautious. Return #W4072946 via paypal_1808675: Action Camera; Hiking Boots; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W4072946",
                    "item_ids": ["5436236388", "8106223139"],
                    "payment_method_id": "paypal_1808675",
                },
            )
        ],
        outputs=[],
    
        uuid="2965ed0d-008c-4d79-86d7-bc7285f5827b",),
    Task(
        annotator="synthetic",
        user_id="juan_kim_6026",
        instruction="Your name is Juan Kim and your email is juan.kim2574@example.com. You are flexible, dependent. Return #W2002172 via paypal_5061070: Cycling Helmet; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W2002172",
                    "item_ids": ["9013366374"],
                    "payment_method_id": "paypal_5061070",
                },
            )
        ],
        outputs=[],
    
        uuid="0c9b71cd-f7d7-4f1c-97cb-83c2e7268d81",),
    Task(
        annotator="synthetic",
        user_id="ethan_johnson_7053",
        instruction="Your name is Ethan Johnson and your zip code is 80298. You are sad, outgoing, flexible. Return #W5321777 via gift_card_6892585: Espresso Machine; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W5321777",
                    "item_ids": ["7441167885"],
                    "payment_method_id": "gift_card_6892585",
                },
            )
        ],
        outputs=[],
    
        uuid="96fd2309-237e-4834-856a-e0931497afa6",),
    Task(
        annotator="synthetic",
        user_id="sophia_jackson_7119",
        instruction="Your name is Sophia Jackson and your email is sophia.jackson9875@example.com. You are outgoing, confident. Return #W3977493 via credit_card_6748580: Water Bottle {'capacity': '500ml', 'material': 'stainless steel', 'color': 'green'}; Electric Toothbrush; Laptop; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W3977493",
                    "item_ids": ["7533802601", "7144237253", "2216662955"],
                    "payment_method_id": "credit_card_6748580",
                },
            )
        ],
        outputs=[],
    
        uuid="52dddf69-3fe1-4dcf-add5-9bab015a360b",),
    Task(
        annotator="synthetic",
        user_id="mia_garcia_4516",
        instruction="Your name is Mia Garcia and your zip code is 46229. You are independent, direct, flexible. Return #W5490111 via credit_card_3124723: Action Camera; Backpack; Water Bottle; Mechanical Keyboard; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W5490111",
                    "item_ids": [
                        "6117189161",
                        "4947717507",
                        "4579334072",
                        "1421289881",
                    ],
                    "payment_method_id": "credit_card_3124723",
                },
            )
        ],
        outputs=[],
    
        uuid="d790dbf3-cdd6-49ba-878b-f8ede9afeb61",),
    Task(
        annotator="synthetic",
        user_id="mia_smith_1623",
        instruction="Your name is Mia Smith and your zip code is 80246. You are logical, independent, direct, impatient, sad. Return #W2922379 via paypal_3839332: Water Bottle; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W2922379",
                    "item_ids": ["7661609223"],
                    "payment_method_id": "paypal_3839332",
                },
            )
        ],
        outputs=[],
    
        uuid="1e077188-f051-43f9-900f-5624d956a182",),
    Task(
        annotator="synthetic",
        user_id="chen_johnson_4204",
        instruction="Your name is Chen Johnson and your email is chen.johnson3889@example.com. You are happy, flexible, impatient, shy, messy. Return #W5797164 via gift_card_3406421: Jigsaw Puzzle; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W5797164",
                    "item_ids": ["9237024510"],
                    "payment_method_id": "gift_card_3406421",
                },
            )
        ],
        outputs=[],
    
        uuid="b0fc3922-d0c6-42ea-b5d3-42d59625487b",),
    Task(
        annotator="synthetic",
        user_id="fatima_brown_5229",
        instruction="Your name is Fatima Brown and your email is fatima.brown7817@example.com. You are pessimistic, rigid. Return #W9045919 via gift_card_8633125: Smart Thermostat; Digital Camera; Cycling Helmet; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W9045919",
                    "item_ids": ["4953074738", "1804581713", "1719127154"],
                    "payment_method_id": "gift_card_8633125",
                },
            )
        ],
        outputs=[],
    
        uuid="a2d95b10-47a0-4017-9b98-13409c77d41d",),
    Task(
        annotator="synthetic",
        user_id="mason_wilson_4597",
        instruction="Your name is Mason Wilson and your email is mason.wilson6954@example.com. You are dependent, cautious, shy. Return #W8161562 via gift_card_6767859: Digital Camera; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W8161562",
                    "item_ids": ["7195021808"],
                    "payment_method_id": "gift_card_6767859",
                },
            )
        ],
        outputs=[],
    
        uuid="0b450c42-7c7a-43e3-b313-3fad34891584",),
    Task(
        annotator="synthetic",
        user_id="lei_hernandez_8500",
        instruction="Your name is Lei Hernandez and your zip code is 43222. You are shy, curious, polite, dependent. Return #W6146740 via gift_card_5245016: Hiking Boots; Laptop; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W6146740",
                    "item_ids": ["8118291112", "6056040996"],
                    "payment_method_id": "gift_card_5245016",
                },
            )
        ],
        outputs=[],
    
        uuid="2792a611-c833-44fa-aecb-56d78c256cdb",),
    Task(
        annotator="synthetic",
        user_id="harper_li_7655",
        instruction="Your name is Harper Li and your zip code is 32253. You are happy, pessimistic. Return #W9495141 via gift_card_8862145: Tablet; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W9495141",
                    "item_ids": ["6501071631"],
                    "payment_method_id": "gift_card_8862145",
                },
            )
        ],
        outputs=[],
    
        uuid="402f5657-b555-476e-b641-a9ce3954f291",),
    Task(
        annotator="synthetic",
        user_id="aarav_moore_6923",
        instruction="Your name is Aarav Moore and your zip code is 85041. You are independent, rigid, creative, confident. Return #W8496475 via paypal_4751854: Tea Kettle; Headphones; Perfume; Water Bottle; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W8496475",
                    "item_ids": [
                        "7274158061",
                        "9314474252",
                        "6826843914",
                        "3229676465",
                    ],
                    "payment_method_id": "paypal_4751854",
                },
            )
        ],
        outputs=[],
    
        uuid="07de93d8-74d4-4f95-b9d3-2276a5a497fd",),
    Task(
        annotator="synthetic",
        user_id="harper_kovacs_9747",
        instruction="Your name is Harper Kovacs and your zip code is 10206. You are busy, independent, happy, direct. Return #W6221400 via gift_card_5087631: Air Purifier; Water Bottle; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W6221400",
                    "item_ids": ["4035304400", "7843064651"],
                    "payment_method_id": "gift_card_5087631",
                },
            )
        ],
        outputs=[],
    
        uuid="91ba5a98-c911-4022-8d58-0705513df69d",),
    Task(
        annotator="synthetic",
        user_id="ethan_thomas_1791",
        instruction="Your name is Ethan Thomas and your email is ethan.thomas7730@example.com. You are direct, outgoing, impatient. Return #W7764382 via gift_card_2519457: Mechanical Keyboard; Pet Bed; Indoor Security Camera; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W7764382",
                    "item_ids": ["9665000388", "5067898160", "3909704820"],
                    "payment_method_id": "gift_card_2519457",
                },
            )
        ],
        outputs=[],
    
        uuid="6cd166f6-36e7-4a32-be7d-223776c69f48",),
    Task(
        annotator="synthetic",
        user_id="olivia_ahmed_6778",
        instruction="Your name is Olivia Ahmed and your email is olivia.ahmed5620@example.com. You are organized, happy, creative. Return #W1579621 via credit_card_9698900: Water Bottle; Mechanical Keyboard; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W1579621",
                    "item_ids": ["4579334072", "6439196450"],
                    "payment_method_id": "credit_card_9698900",
                },
            )
        ],
        outputs=[],
    
        uuid="c60c3062-9b80-46eb-b046-b1791150cd4c",),
    Task(
        annotator="synthetic",
        user_id="liam_johnson_5676",
        instruction="Your name is Liam Johnson and your zip code is 46244. You are messy, pessimistic, relaxing. Return #W7190291 via credit_card_7120747: Headphones; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W7190291",
                    "item_ids": ["7184044281"],
                    "payment_method_id": "credit_card_7120747",
                },
            )
        ],
        outputs=[],
    
        uuid="34c1c90d-5652-4e04-9093-86107389f875",),
    Task(
        annotator="synthetic",
        user_id="yara_davis_8348",
        instruction="Your name is Yara Davis and your zip code is 92122. You are curious, logical, insecure. Return #W3952055 via credit_card_1248375: Dumbbell Set; Makeup Kit; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W3952055",
                    "item_ids": ["3333391894", "7902309762"],
                    "payment_method_id": "credit_card_1248375",
                },
            )
        ],
        outputs=[],
    
        uuid="03ada3ab-362b-4bd4-aa35-747ae1972dca",),
    Task(
        annotator="synthetic",
        user_id="yara_santos_1202",
        instruction="Your name is Yara Santos and your zip code is 91163. You are pessimistic, creative. Return #W3232025 via gift_card_4543462: Dumbbell Set; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W3232025",
                    "item_ids": ["2444431651"],
                    "payment_method_id": "gift_card_4543462",
                },
            )
        ],
        outputs=[],
    
        uuid="312ddb84-e325-4d90-ae03-c6d5cd6732ab",),
    Task(
        annotator="synthetic",
        user_id="mia_thomas_4629",
        instruction="Your name is Mia Thomas and your zip code is 60654. You are independent, confident. Return #W6872071 via paypal_2977884: Bluetooth Speaker; LED Light Bulb; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W6872071",
                    "item_ids": ["4716977452", "7445824652"],
                    "payment_method_id": "paypal_2977884",
                },
            )
        ],
        outputs=[],
    
        uuid="827ddcc0-bdaa-4619-a6c4-b1bb4ced00dc",),
    Task(
        annotator="synthetic",
        user_id="lei_hernandez_8500",
        instruction="Your name is Lei Hernandez and your zip code is 43222. You are impatient, independent, confident. Return #W2982823 via gift_card_5245016: Cycling Helmet; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W2982823",
                    "item_ids": ["1719127154"],
                    "payment_method_id": "gift_card_5245016",
                },
            )
        ],
        outputs=[],
    
        uuid="5527d5a1-b434-4254-a462-ca3706b339b6",),
    Task(
        annotator="synthetic",
        user_id="ava_johnson_5052",
        instruction="Your name is Ava Johnson and your zip code is 92171. You are relaxing, insecure, creative, independent. Return #W9178204 via paypal_3846161: Desk Lamp; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W9178204",
                    "item_ids": ["6805564527"],
                    "payment_method_id": "paypal_3846161",
                },
            )
        ],
        outputs=[],
    
        uuid="95023a09-0793-4252-80ff-321112693496",),
    Task(
        annotator="synthetic",
        user_id="raj_davis_2615",
        instruction="Your name is Raj Davis and your email is raj.davis3587@example.com. You are busy, patient, dependent, messy, sad. Return #W5463717 via gift_card_8006222: Grill; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W5463717",
                    "item_ids": ["6589665742"],
                    "payment_method_id": "gift_card_8006222",
                },
            )
        ],
        outputs=[],
    
        uuid="47c3f297-a293-4285-844a-11a9368b3a54",),
    Task(
        annotator="synthetic",
        user_id="mohamed_santos_2427",
        instruction="Your name is Mohamed Santos and your zip code is 76188. You are pessimistic, sad, shy, rigid. Return #W4840405 via gift_card_4710915: Luggage Set; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W4840405",
                    "item_ids": ["6301799585"],
                    "payment_method_id": "gift_card_4710915",
                },
            )
        ],
        outputs=[],
    
        uuid="a02c0021-c6fe-4ddc-9a03-1be5847e5d04",),
    Task(
        annotator="synthetic",
        user_id="isabella_johnson_6293",
        instruction="Your name is Isabella Johnson and your zip code is 98119. You are impatient, logical, messy, curious, direct. Return #W3431083 via paypal_5071744: Wireless Earbuds; Backpack; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W3431083",
                    "item_ids": ["3694871183", "6309044598"],
                    "payment_method_id": "paypal_5071744",
                },
            )
        ],
        outputs=[],
    
        uuid="14a699e0-99f3-4b24-bb7a-3d9255ed46e0",),
    Task(
        annotator="synthetic",
        user_id="anya_kovacs_9542",
        instruction="Your name is Anya Kovacs and your zip code is 95132. You are busy, polite, dependent, outgoing, curious. Return #W6821773 via credit_card_4829249: Fleece Jacket; Office Chair; Cycling Helmet; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W6821773",
                    "item_ids": ["8590708195", "2386562819", "6048672633"],
                    "payment_method_id": "credit_card_4829249",
                },
            )
        ],
        outputs=[],
    
        uuid="e1b39985-f8f9-4451-a871-595b1daad3c5",),
    Task(
        annotator="synthetic",
        user_id="james_li_5688",
        instruction="Your name is James Li and your zip code is 10083. You are pessimistic, confident, relaxing. Return #W3638028 via gift_card_1725971: Jigsaw Puzzle; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W3638028",
                    "item_ids": ["4572024853"],
                    "payment_method_id": "gift_card_1725971",
                },
            )
        ],
        outputs=[],
    
        uuid="33e6d793-f1d5-4492-958e-f262c7c40d1a",),
    Task(
        annotator="synthetic",
        user_id="sophia_garcia_5025",
        instruction="Your name is Sophia Garcia and your zip code is 20156. You are confident, cautious, rigid. Return #W5777276 via credit_card_4147840: Bookshelf; Notebook; Tablet; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W5777276",
                    "item_ids": ["7154215719", "7579176349", "2106335193"],
                    "payment_method_id": "credit_card_4147840",
                },
            )
        ],
        outputs=[],
    
        uuid="f2ec2faa-fbac-4511-bd8c-de559ce25600",),
    Task(
        annotator="synthetic",
        user_id="sophia_lee_8294",
        instruction="Your name is Sophia Lee and your email is sophia.lee4144@example.com. You are cautious, logical. Return #W7366745 via gift_card_7803378: Grill; Sunglasses; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W7366745",
                    "item_ids": ["7848293342", "9672174103"],
                    "payment_method_id": "gift_card_7803378",
                },
            )
        ],
        outputs=[],
    
        uuid="cbab813a-896f-4416-bacd-23b583450481",),
    Task(
        annotator="synthetic",
        user_id="yara_johansson_9032",
        instruction="Your name is Yara Johansson and your email is yara.johansson5198@example.com. You are shy, creative. Return #W6904184 via credit_card_6699629: Electric Kettle; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W6904184",
                    "item_ids": ["8142779083"],
                    "payment_method_id": "credit_card_6699629",
                },
            )
        ],
        outputs=[],
    
        uuid="58093ee5-7eed-4596-b8bc-ed46309c9044",),
    Task(
        annotator="synthetic",
        user_id="aarav_sanchez_6636",
        instruction="Your name is Aarav Sanchez and your email is aarav.sanchez5467@example.com. You are direct, outgoing, optimistic, flexible. Return #W9552705 via gift_card_8922351: Bookshelf; Portable Charger; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W9552705",
                    "item_ids": ["2244749153", "1178356107"],
                    "payment_method_id": "gift_card_8922351",
                },
            )
        ],
        outputs=[],
    
        uuid="2e30bd01-898b-4a57-8d67-5ea91b1b2db8",),
    Task(
        annotator="synthetic",
        user_id="ivan_johnson_6036",
        instruction="Your name is Ivan Johnson and your email is ivan.johnson5749@example.com. You are rigid, happy, optimistic, insecure. Return #W1671835 via paypal_6918118: Perfume; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W1671835",
                    "item_ids": ["5081446110"],
                    "payment_method_id": "paypal_6918118",
                },
            )
        ],
        outputs=[],
    
        uuid="e2817585-e19b-412d-b989-10d256772eab",),
    Task(
        annotator="synthetic",
        user_id="yusuf_patel_7767",
        instruction="Your name is Yusuf Patel and your zip code is 94117. You are curious, organized, independent, confident, relaxing. Return #W2274128 via gift_card_3372949: Hiking Boots; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W2274128",
                    "item_ids": ["2185126308"],
                    "payment_method_id": "gift_card_3372949",
                },
            )
        ],
        outputs=[],
    
        uuid="b5607719-3ca7-4213-9a02-5689eb35a25a",),
    Task(
        annotator="synthetic",
        user_id="ethan_lopez_6291",
        instruction="Your name is Ethan Lopez and your email is ethan.lopez8943@example.com. You are shy, cautious. Return #W8632528 via gift_card_7219486: Backpack; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W8632528",
                    "item_ids": ["5917587651"],
                    "payment_method_id": "gift_card_7219486",
                },
            )
        ],
        outputs=[],
    
        uuid="f0e83f59-dfd7-40a8-9a0b-e5da1e71d2b0",),
    Task(
        annotator="synthetic",
        user_id="omar_moore_9540",
        instruction="Your name is Omar Moore and your zip code is 10096. You are organized, busy, shy, logical. Return #W1874267 via credit_card_8008637: Digital Camera; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W1874267",
                    "item_ids": ["2284404181"],
                    "payment_method_id": "credit_card_8008637",
                },
            )
        ],
        outputs=[],
    
        uuid="253a3bac-ae17-4ad6-b828-b532f0275f58",),
    Task(
        annotator="synthetic",
        user_id="ethan_moore_9003",
        instruction="Your name is Ethan Moore and your zip code is 75339. You are direct, independent, outgoing. Return #W6026015 via credit_card_6361025: Dumbbell Set; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W6026015",
                    "item_ids": ["6130713659"],
                    "payment_method_id": "credit_card_6361025",
                },
            )
        ],
        outputs=[],
    
        uuid="e29a3ab8-6f64-4936-9b7f-3d980949117e",),
    Task(
        annotator="synthetic",
        user_id="ethan_kim_8860",
        instruction="Your name is Ethan Kim and your email is ethan.kim3231@example.com. You are messy, relaxing, independent. Return #W3942875 via gift_card_5701566: Running Shoes; Water Bottle; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W3942875",
                    "item_ids": ["1775591963", "2366567022"],
                    "payment_method_id": "gift_card_5701566",
                },
            )
        ],
        outputs=[],
    
        uuid="f30046cf-34f8-4fb0-a9ed-168e3c1efcff",),
    Task(
        annotator="synthetic",
        user_id="noah_li_2316",
        instruction="Your name is Noah Li and your email is noah.li7327@example.com. You are polite, pessimistic, confident, outgoing, patient. Return #W8553554 via credit_card_4467209: Pet Bed; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W8553554",
                    "item_ids": ["4537595158"],
                    "payment_method_id": "credit_card_4467209",
                },
            )
        ],
        outputs=[],
    
        uuid="040e45dd-db27-46be-bef8-e87203767b5d",),
    Task(
        annotator="synthetic",
        user_id="omar_moore_9540",
        instruction="Your name is Omar Moore and your zip code is 10096. You are impatient, independent. Return #W1874267 via credit_card_8008637: Digital Camera; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W1874267",
                    "item_ids": ["2284404181"],
                    "payment_method_id": "credit_card_8008637",
                },
            )
        ],
        outputs=[],
    
        uuid="422687fa-ae38-4623-b9c0-33b4a521b1ab",),
    Task(
        annotator="synthetic",
        user_id="amelia_silva_7726",
        instruction="Your name is Amelia Silva and your email is amelia.silva7872@example.com. You are shy, direct. Return #W7773202 via gift_card_3491931: Hiking Boots; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W7773202",
                    "item_ids": ["8277474082"],
                    "payment_method_id": "gift_card_3491931",
                },
            )
        ],
        outputs=[],
    
        uuid="de72f4f1-4335-402c-89c0-55809e9bdff9",),
    Task(
        annotator="synthetic",
        user_id="yusuf_jackson_7865",
        instruction="Your name is Yusuf Jackson and your zip code is 98127. You are impatient, independent, busy. Return #W7128968 via paypal_3392566: Vacuum Cleaner; Bluetooth Speaker; Pet Bed; Bookshelf; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W7128968",
                    "item_ids": [
                        "6259501109",
                        "2652637226",
                        "7729002517",
                        "7539442683",
                    ],
                    "payment_method_id": "paypal_3392566",
                },
            )
        ],
        outputs=[],
    
        uuid="3f5b121a-81d3-4476-adb0-50ed6bc29a40",),
    Task(
        annotator="synthetic",
        user_id="olivia_davis_3316",
        instruction="Your name is Olivia Davis and your email is olivia.davis4495@example.com. You are sad, independent, busy, polite, patient. Return #W7623533 via paypal_8673863: Jigsaw Puzzle; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W7623533",
                    "item_ids": ["4772738468"],
                    "payment_method_id": "paypal_8673863",
                },
            )
        ],
        outputs=[],
    
        uuid="be56777a-5b7a-44f7-b97c-4c8279a9f214",),
    Task(
        annotator="synthetic",
        user_id="olivia_brown_4616",
        instruction="Your name is Olivia Brown and your zip code is 43118. You are relaxing, sad, organized, flexible, curious. Return #W2912153 via credit_card_3081930: Electric Kettle; Desk Lamp; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W2912153",
                    "item_ids": ["9472539378", "1270145486"],
                    "payment_method_id": "credit_card_3081930",
                },
            )
        ],
        outputs=[],
    
        uuid="3cae85ef-6a28-4b4f-85d4-b4ae66efa440",),
    Task(
        annotator="synthetic",
        user_id="noah_brown_6181",
        instruction="Your name is Noah Brown and your email is noah.brown7922@example.com. You are happy, messy, confident, cautious. Return #W7678072 via paypal_5727330: Gaming Mouse; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W7678072",
                    "item_ids": ["2193628750"],
                    "payment_method_id": "paypal_5727330",
                },
            )
        ],
        outputs=[],
    
        uuid="27136bdc-4b49-4019-88a0-6bd4a7c228a3",),
    Task(
        annotator="synthetic",
        user_id="aarav_sanchez_6636",
        instruction="Your name is Aarav Sanchez and your zip code is 60653. You are patient, busy, messy. Return #W9552705 via gift_card_8922351: Cycling Helmet; Bookshelf; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W9552705",
                    "item_ids": ["6697922351", "2244749153"],
                    "payment_method_id": "gift_card_8922351",
                },
            )
        ],
        outputs=[],
    
        uuid="406d146c-75b9-47a4-b967-94effb4541ea",),
    Task(
        annotator="synthetic",
        user_id="sophia_garcia_5025",
        instruction="Your name is Sophia Garcia and your zip code is 20156. You are messy, curious, relaxing, direct, patient. Return #W5777276 via credit_card_4147840: Tablet; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W5777276",
                    "item_ids": ["2106335193"],
                    "payment_method_id": "credit_card_4147840",
                },
            )
        ],
        outputs=[],
    
        uuid="6fa578ae-59ae-404d-ad27-0d4a78377bb8",),
    Task(
        annotator="synthetic",
        user_id="mia_garcia_4516",
        instruction="Your name is Mia Garcia and your email is mia.garcia2723@example.com. You are impatient, insecure, outgoing. Return #W5490111 via credit_card_3124723: Action Camera; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W5490111",
                    "item_ids": ["6117189161"],
                    "payment_method_id": "credit_card_3124723",
                },
            )
        ],
        outputs=[],
    
        uuid="b7a2c5ae-b483-4d04-913f-15610a18ef38",),
    Task(
        annotator="synthetic",
        user_id="ethan_thomas_1791",
        instruction="Your name is Ethan Thomas and your zip code is 43188. You are confident, polite, busy, curious. Return #W7764382 via gift_card_2519457: Indoor Security Camera; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W7764382",
                    "item_ids": ["3909704820"],
                    "payment_method_id": "gift_card_2519457",
                },
            )
        ],
        outputs=[],
    
        uuid="c51bbb9a-7b5f-486b-9ca0-e18169efe6ab",),
    Task(
        annotator="synthetic",
        user_id="yusuf_hernandez_6467",
        instruction="Your name is Yusuf Hernandez and your email is yusuf.hernandez6086@example.com. You are creative, dependent, patient, confident. Return #W7133840 via paypal_9426036: Bookshelf; Backpack; Jigsaw Puzzle; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W7133840",
                    "item_ids": ["6735339143", "4947717507", "7127170374"],
                    "payment_method_id": "paypal_9426036",
                },
            )
        ],
        outputs=[],
    
        uuid="ab58f650-bbee-40ed-a84f-7a7399ba0381",),
    Task(
        annotator="synthetic",
        user_id="yusuf_rossi_9620",
        instruction="Your name is Yusuf Rossi and your email is yusuf.rossi7301@example.com. You are patient, happy. Return #W6679257 via credit_card_9513926: Digital Camera; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W6679257",
                    "item_ids": ["5996159312"],
                    "payment_method_id": "credit_card_9513926",
                },
            )
        ],
        outputs=[],
    
        uuid="115f8f18-a56f-4a28-b8bd-055445ed68db",),
    Task(
        annotator="synthetic",
        user_id="ethan_johnson_7053",
        instruction="Your name is Ethan Johnson and your email is ethan.johnson2557@example.com. You are impatient, direct, rigid, pessimistic, outgoing. Return #W7450915 via gift_card_6892585: Laptop; Bookshelf; Digital Camera; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W7450915",
                    "item_ids": ["3334537816", "6735339143", "7195021808"],
                    "payment_method_id": "gift_card_6892585",
                },
            )
        ],
        outputs=[],
    
        uuid="fd336815-cc39-47cc-8acf-4ae1a3c8d38d",),
    Task(
        annotator="synthetic",
        user_id="ethan_sanchez_2952",
        instruction="Your name is Ethan Sanchez and your email is ethan.sanchez6360@example.com. You are cautious, insecure. Return #W9250394 via gift_card_4817478: Wristwatch; Dumbbell Set; Smart Watch; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W9250394",
                    "item_ids": ["2407258246", "7159180318", "2681513500"],
                    "payment_method_id": "gift_card_4817478",
                },
            )
        ],
        outputs=[],
    
        uuid="9a53b66b-e324-498d-8d83-3ea024dcc87c",),
    Task(
        annotator="synthetic",
        user_id="daiki_kim_2165",
        instruction="Your name is Daiki Kim and your email is daiki.kim7376@example.com. You are relaxing, logical, shy. Return #W4824466 via gift_card_9919420: Headphones; Hiking Boots; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W4824466",
                    "item_ids": ["5635439102", "8106223139"],
                    "payment_method_id": "gift_card_9919420",
                },
            )
        ],
        outputs=[],
    
        uuid="c9c7bca8-b394-480b-86c8-46ab4533c16a",),
    Task(
        annotator="synthetic",
        user_id="mia_smith_1623",
        instruction="Your name is Mia Smith and your email is mia.smith4644@example.com. You are outgoing, dependent, rigid, happy, patient. Return #W5254379 via paypal_3839332: Air Purifier; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W5254379",
                    "item_ids": ["4035304400"],
                    "payment_method_id": "paypal_3839332",
                },
            )
        ],
        outputs=[],
    
        uuid="4c6aa99c-a343-47e5-862d-b60b03166141",),
    Task(
        annotator="synthetic",
        user_id="lucas_brown_6720",
        instruction="Your name is Lucas Brown and your email is lucas.brown9344@example.com. You are happy, optimistic, outgoing, relaxing. Return #W9218746 via credit_card_2112420: Vacuum Cleaner; Backpack; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W9218746",
                    "item_ids": ["2872451762", "7824298782"],
                    "payment_method_id": "credit_card_2112420",
                },
            )
        ],
        outputs=[],
    
        uuid="44be3953-fece-4465-a729-abcc2c6202f3",),
    Task(
        annotator="synthetic",
        user_id="raj_santos_9079",
        instruction="Your name is Raj Santos and your zip code is 98157. You are rigid, busy. Return #W1630030 via paypal_2417743: Electric Kettle; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W1630030",
                    "item_ids": ["4458619711"],
                    "payment_method_id": "paypal_2417743",
                },
            )
        ],
        outputs=[],
    
        uuid="0ed9c7c1-a4cd-4f09-bbcc-d021c82f9b03",),
    Task(
        annotator="synthetic",
        user_id="omar_khan_2363",
        instruction="Your name is Omar Khan and your email is omar.khan3563@example.com. You are direct, outgoing, independent, relaxing. Return #W6304490 via credit_card_4420174: Skateboard; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W6304490",
                    "item_ids": ["6956751343"],
                    "payment_method_id": "credit_card_4420174",
                },
            )
        ],
        outputs=[],
    
        uuid="a25c0288-dc65-4f35-85aa-d222c476d9df",),
    Task(
        annotator="synthetic",
        user_id="harper_li_7655",
        instruction="Your name is Harper Li and your email is harper.li3262@example.com. You are patient, direct, confident. Return #W9495141 via gift_card_8862145: Tablet; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W9495141",
                    "item_ids": ["6501071631"],
                    "payment_method_id": "gift_card_8862145",
                },
            )
        ],
        outputs=[],
    
        uuid="d7938627-e225-45ad-bb12-d1111c513da0",),
    Task(
        annotator="synthetic",
        user_id="noah_brown_6181",
        instruction="Your name is Noah Brown and your zip code is 80279. You are polite, rigid. Return #W7678072 via paypal_5727330: Backpack; Electric Kettle; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W7678072",
                    "item_ids": ["3557711149", "2323972008"],
                    "payment_method_id": "paypal_5727330",
                },
            )
        ],
        outputs=[],
    
        uuid="2fc9b2ec-dfa7-4b22-9934-650de5bbb21e",),
    Task(
        annotator="synthetic",
        user_id="lei_khan_6353",
        instruction="Your name is Lei Khan and your zip code is 92182. You are organized, cautious, confident, shy, busy. Return #W2787996 via gift_card_6786837: T-Shirt; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W2787996",
                    "item_ids": ["9354168549"],
                    "payment_method_id": "gift_card_6786837",
                },
            )
        ],
        outputs=[],
    
        uuid="37fc9769-133d-4a68-a51c-6a3effe2eb76",),
    Task(
        annotator="synthetic",
        user_id="mei_moore_8248",
        instruction="Your name is Mei Moore and your zip code is 90980. You are pessimistic, rigid, busy, insecure. Return #W9694847 via credit_card_2902980: Air Purifier; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W9694847",
                    "item_ids": ["5669664287"],
                    "payment_method_id": "credit_card_2902980",
                },
            )
        ],
        outputs=[],
    
        uuid="a1fb7810-e5b2-4a7b-a65e-550f4d141536",),
    Task(
        annotator="synthetic",
        user_id="juan_santos_1448",
        instruction="Your name is Juan Santos and your email is juan.santos3161@example.com. You are outgoing, impatient, independent, cautious. Return #W2582045 via gift_card_3767667: Air Purifier; ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W2582045",
                    "item_ids": ["5669664287"],
                    "payment_method_id": "gift_card_3767667",
                },
            )
        ],
        outputs=[],
    
        uuid="e0452438-ec7f-42ef-988c-feb26e81f16f",),
    Task(
        annotator="4",
        user_id="chen_silva_7485",
        instruction="You name is Chen Silva and your zip code is 46281. You are messy, flexible, outgoing. You received two tablets and you only need one. You want to return the more expensive one and refund to credit card. If refund to credit card is not possible, you become angry and return everything on that order and refund to GC.",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W9571698",
                    "item_ids": [
                        "5952720925",
                        "9973034634",
                        "7381052709",
                        "6065192424",
                    ],
                    "payment_method_id": "gift_card_7250692",
                },
            )
        ],
        outputs=[],
    
        uuid="31ed17e1-0753-4301-8c42-501417b87a27",),
    Task(
        annotator="4",
        user_id="chen_silva_7485",
        instruction="You name is Chen Silva and your zip code is 46281. You are messy, flexible, outgoing. You received two tablets and you only need one. You want to return the more expensive one and refund to credit card. If refund to credit card is not possible, you become angry and refund to GC.",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W9571698",
                    "item_ids": ["6065192424"],
                    "payment_method_id": "gift_card_7250692",
                },
            )
        ],
        outputs=[],
    
        uuid="1fb88b30-1f25-4e10-bdbb-47aba9e72dff",),
    Task(
        annotator="4",
        user_id="chen_silva_7485",
        instruction="You name is Chen Silva and your zip code is 46281. You are messy, flexible, outgoing. You received two tablets and you only need one. You want to return the less expensive one and refund to credit card. But if the agent asks for confirmation, you change your mind and return the more expensive one and refund to GC.",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W9571698",
                    "item_ids": ["6065192424"],
                    "payment_method_id": "gift_card_7250692",
                },
            )
        ],
        outputs=[],
    
        uuid="cd377a33-7477-49ef-b2c4-01aa59d49d3a",),
    Task(
        annotator="4",
        user_id="raj_santos_9079",
        instruction="You name is Raj Santos and your zip code is 98157. You are dependent, flexible. You want to know what is the cheapest availabe mechanical keyboard right now and its options. If it is less than 200 bucks you want to exchange your current one to it. If not, return your current one.",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W4680753",
                    "item_ids": ["9690244451"],
                    "payment_method_id": "paypal_2417743",
                },
            )
        ],
        outputs=["226.11", "tactile", "white", "full"],
    
        uuid="b2393af5-2166-4a9f-a98b-d802b40e4ff3",),
    Task(
        annotator="4",
        user_id="yusuf_gonzalez_8900",
        instruction="You name is Yusuf Gonzalez and your zip code is 91455. You want to return everything but a tablet in a recently delivered order. ",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W1679211",
                    "item_ids": ["9612497925", "7127170374", "6268080249"],
                    "payment_method_id": "paypal_3022415",
                },
            )
        ],
        outputs=[],
    
        uuid="2b84043c-bdb9-4654-be85-4deeddd407ba",),
    Task(
        annotator="",
        user_id="liam_li_5260",
        instruction="Your name is Liam Li and your email is liam.li2557@example.com. You are insecure, outgoing, sad, impatient. You received the skateboard that you've ordered a week ago but you used the skateboard only once, and the board is already chipped. You wanna make sure that you're still eligible to receive a full refund even though you've used the skateboard once.",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W8512927",
                    "item_ids": ["5120532699"],
                    "payment_method_id": "credit_card_7933535",
                },
            )
        ],
        outputs=[],
    
        uuid="46e66525-76ce-4d10-af1b-b86875d8cd94",),
    Task(
        annotator="",
        user_id="olivia_ito_3591",
        instruction="Your name is Olivia Ito and your zip code is 80218. You are relaxing, impatient, direct, organized, curious. Return the all the items from the order (the order contained Sneakers and a Espresso Machine). You're initially unsure which payment method to use for the refund, try to get more information about the payment methods available for the refund. You eventually decide to get a gift card for the refund.",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W5866402",
                    "item_ids": ["9727387530", "6242772310"],
                    "payment_method_id": "gift_card_7794233",
                },
            )
        ],
        outputs=[],
    
        uuid="b433cac3-3c01-4578-8ff7-4fce81be41db",),
    Task(
        annotator="",
        user_id="ivan_santos_6635",
        instruction="Your name is Ivan Santos and your email is ivan.santos3158@example.com. You are pessimistic, cautious, patient, dependent, shy. The packaging of the order that you received (#W6893533) was damaged and left in rain and it was all wet when you received it. You're worried that the items inside the package might be damaged. You want to return the items and get a full refund. You're also worried that the return process might be complicated and you want to make sure that the return process is easy.",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W6893533",
                    "item_ids": ["5206946487", "1646531091"],
                    "payment_method_id": "paypal_6151711",
                },
            )
        ],
        outputs=[],
    
        uuid="e6c0bf84-3221-4bad-b966-6069116477dc",),
    Task(
        annotator="",
        user_id="aarav_sanchez_6636",
        instruction="Your name is Aarav Sanchez and your email is aarav.sanchez5467@example.com. You are patient, shy. Return the Portable Charger of your order. But before confirming, decide to return the Bookshelf and the Cycling Helmet as well. You wanna get website credit for the return.",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W9552705",
                    "item_ids": ["1178356107", "2244749153", "6697922351"],
                    "payment_method_id": "gift_card_8922351",
                },
            )
        ],
        outputs=[],
    
        uuid="ed2c29a9-948a-464a-b504-cb21dfd0844a",),
    Task(
        annotator="",
        user_id="isabella_brown_3584",
        instruction="Your name is Isabella Brown and your zip code is 80257. You are patient, shy, insecure, rigid. The jigsaw puzzle that you've recently received is missing pieces and you're very disappointed. You're sure that the piece was missing on delivery. Because of the missing piece, you don't want to keep the puzzle and wanna get a full refund via paypal. Try your best to get a coupon for the next purchase you make because of the inconvenience. If you can't get a coupon, try to talk to the supervisor and insist on getting a coupon for the hassle that you've been through.",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W7752779",
                    "item_ids": ["4068787148"],
                    "payment_method_id": "paypal_2143483",
                },
            )
        ],
        outputs=[],
    
        uuid="a2dae93f-6347-43f5-aea1-36481a2058f6",),
    Task(
        annotator="",
        user_id="fatima_smith_4908",
        instruction="Your name is Fatima Smith and your email is fatima.smith9435@example.com. You are shy, independent, pessimistic. The earbuds that you've received doesn't pair with your iPhone. You've been trying to reset your phone multiple times, but it still doesn't work reliably. Try to see if they can troubleshoot the issue, but every time they ask you to do to do something, tell that the you've already tried it and it didn't work. You're sure that the earbuds are faulty and want a full refund.",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W3508684",
                    "item_ids": ["3694871183"],
                    "payment_method_id": "paypal_1575973",
                },
            )
        ],
        outputs=[],
    
        uuid="f709d652-4300-41b9-ac1e-18032ef8bbf5",),
    Task(
        annotator="",
        user_id="mohamed_khan_3010",
        instruction="Your name is Mohamed Khan and your zip code is 60651. You are messy, impatient, busy. You bought a Skateboard recently for around $200 but you realize that the same exact skateboard is available for $150 at another store. You're very disappointed and want to return the skateboard and get a full refund. You're also very busy and don't have time to go to the store to return the item, so you want to return the item via mail. You're also very impatient and want the refund to be processed as soon as possible. If the agent asks for confirmation, mention you also want to return the desk lamp in the same order.",
        actions=[
            Action(
                name="return_delivered_order_items",
                kwargs={
                    "order_id": "#W4887592",
                    "item_ids": ["4447749792", "2343503231"],
                    "payment_method_id": "paypal_1249653",
                },
            )
        ],
        outputs=[],
    
        uuid="158d1479-d3f6-4235-9359-3cec01524d53",)

    
]