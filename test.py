content = open('app.py', 'r', encoding='utf-8').read()
cnt = content.count('"""')
print("Total quotes:", cnt)
