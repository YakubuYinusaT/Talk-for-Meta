"""The generic phrases of a service the front desk reaches (the health provider systems): what it asks, what it answers, and that it cannot be reached.

Merged into templates.EN and CONTEXT when templates loads, so they are translated, spoken and rotated like every other phrase.
"""

EN: dict[str, list[str]] = {'partner_ask_what': ['I can help with {service}. What would you like? For example: {options}.'],
 'partner_say': ['{answer}'],
 'partner_answer': ['{answer} Is there anything else I can help with?'],
 'partner_unavailable': ['I am sorry, {service} is not available right now. Please try again in a little while. Is there anything else I can help '
                         'with?']}

CONTEXT: dict[str, str] = {'partner_ask_what': 'The caller asked for a service but not what they want from it. {service} is the service. {options} is a short list of what it '
                     'can do, written by the service. Asks what they would like.',
 'partner_say': 'Something the service asks the caller. {answer} is the question, already written by the service. Say it as it is.',
 'partner_answer': "What a service told the caller, then asks if there is anything else. {answer} is the service's own sentence: keep it exactly.",
 'partner_unavailable': 'A service the caller asked for cannot be reached right now. {service} is its name. Apologises, says to try again in a '
                        'little while, and asks if there is anything else.'}
