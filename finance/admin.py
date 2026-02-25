from django.contrib import admin
from finance.models import Account, Category, Budget, Transaction, AccountMonthConfig, Workspace, WorkspaceMember

# Register your models here.
admin.site.register(Account)
admin.site.register(Category)
admin.site.register(Budget)
admin.site.register(Transaction)
admin.site.register(AccountMonthConfig)
admin.site.register(Workspace)
admin.site.register(WorkspaceMember)